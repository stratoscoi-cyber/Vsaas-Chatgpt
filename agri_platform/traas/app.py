"""TRAAS Flask application factory (enterprise wiring)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from flask import Flask, jsonify, request
from sqlalchemy import text

from ..common import db as db_helpers
from ..common.config import Settings
from ..common.errors import ApiError, get_json, register_error_handlers, require
from ..common.logging import configure_logging, install_request_logging
from ..common.pagination import page_params, paginate
from ..common.ratelimit import RateLimiter, install_rate_limiting
from ..common.security import install_auth
from . import service
from .models import Base, Hazard, Route, TransportConstraint
from .routing import RoutingEngine, RoutingError, StraightLineEngine, ValhallaEngine

SERVICE_NAME = "traas"
BRAND = "prisaTravel"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _default_engine(settings: Settings) -> RoutingEngine:
    if settings.valhalla_url:
        return ValhallaEngine(url=settings.valhalla_url)
    return StraightLineEngine()


def create_app(
    settings: Optional[Settings] = None,
    session_factory=None,
    routing_engine: Optional[RoutingEngine] = None,
) -> Flask:
    settings = settings or Settings.from_env("traas", default_port=5002)
    configure_logging(settings.log_level, settings.log_json)

    app = Flask(__name__)
    app.config["SETTINGS"] = settings

    if session_factory is None:
        engine, session_factory = db_helpers.make_session_factory(settings.database_url)
        db_helpers.init_models(engine, Base)
    app.config["SESSION_FACTORY"] = session_factory
    app.config["ROUTING_ENGINE"] = routing_engine or _default_engine(settings)

    install_request_logging(app, SERVICE_NAME)
    install_auth(app, settings.api_keys, settings.auth_enabled)
    install_rate_limiting(app, RateLimiter(settings.rate_limit_per_minute))
    register_error_handlers(app)

    @app.teardown_appcontext
    def _cleanup(exc=None):  # noqa: ANN001
        if hasattr(session_factory, "remove"):
            session_factory.remove()

    @app.after_request
    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        return resp

    def db():
        return app.config["SESSION_FACTORY"]()

    def router() -> RoutingEngine:
        return app.config["ROUTING_ENGINE"]

    # --- health & readiness ----------------------------------------------------
    @app.get("/health")
    def health():
        return jsonify(status="healthy", service=SERVICE_NAME, brand=BRAND)

    @app.get("/readyz")
    def readyz():
        try:
            db().execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - infra dependent
            return jsonify(status="not_ready", error=str(exc)), 503
        return jsonify(status="ready", service=SERVICE_NAME)

    # --- routing ---------------------------------------------------------------
    @app.post("/api/routes/safe")
    def safe_route():
        data = get_json()
        require(data, "origin", "destination")
        avoid_rings = data.get("avoid_rings") or service.hazards_to_avoid_rings(data.get("hazards", []))
        try:
            result = router().route(tuple(data["origin"]), tuple(data["destination"]), avoid_rings)
        except RoutingError as exc:
            raise ApiError(str(exc), status_code=502, code="upstream_error")
        return jsonify(status="success", route=result, origin=data["origin"], destination=data["destination"])

    @app.post("/api/routes/optimize")
    def optimize_route():
        data = get_json()
        require(data, "origin", "destination")
        vehicle_type = data.get("vehicle_type", "agricultural_vehicle")
        session = db()
        if "hazards" in data:
            hazards = data["hazards"]
        else:
            hazards = [h.to_dict() for h in session.query(Hazard).filter_by(status="active").all()]
        avoid_rings = service.hazards_to_avoid_rings(hazards)

        try:
            result = router().route(tuple(data["origin"]), tuple(data["destination"]), avoid_rings)
        except RoutingError as exc:
            raise ApiError(str(exc), status_code=502, code="upstream_error")

        route = Route(
            route_id=_new_id("route"),
            origin=data["origin"],
            destination=data["destination"],
            route_type=vehicle_type,
            path=result["coordinates"],
            distance_km=result["distance_km"],
            duration_min=result["duration_min"],
            hazards=hazards,
            status="active",
        )
        session.add(route)
        session.commit()

        safety = service.route_safety(hazards)
        cost = service.transit_cost(result["distance_km"], vehicle_type, len(hazards))
        return jsonify(route=route.to_dict(), safety=safety, cost=cost), 201

    @app.post("/api/routes/analyze")
    def analyze_route():
        data = get_json()
        require(data, "path")
        return jsonify(
            safety=service.route_safety(data.get("hazards", [])),
            distance_km=service.route_distance_km(data["path"]),
        )

    @app.post("/api/routes/distance")
    def route_distance():
        data = get_json()
        coords = data.get("coordinates")
        if not coords or len(coords) < 2:
            raise ApiError("at least 2 coordinates required", status_code=422, code="validation_error")
        return jsonify(distance_km=service.route_distance_km(coords))

    @app.get("/api/routes")
    def list_routes():
        q = db().query(Route).order_by(Route.id.desc())
        if request.args.get("status"):
            q = q.filter_by(status=request.args["status"])
        if request.args.get("type"):
            q = q.filter_by(route_type=request.args["type"])
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(routes=[r.to_dict() for r in items], pagination=meta)

    @app.get("/api/routes/<route_id>")
    def get_route(route_id):
        route = db().query(Route).filter_by(route_id=route_id).first()
        if route is None:
            raise ApiError("route not found", status_code=404, code="not_found")
        return jsonify(route.to_dict())

    # --- hazards ---------------------------------------------------------------
    @app.post("/api/hazards")
    def add_hazard():
        data = get_json()
        require(data, "hazard_type", "location")
        ttl_hours = float(data.get("ttl_hours", 2))
        hazard = Hazard(
            hazard_id=data.get("hazard_id") or _new_id("hz"),
            hazard_type=data["hazard_type"],
            severity=data.get("severity", "Medium"),
            location=data["location"],
            geometry=data.get("geometry"),
            radius_km=float(data.get("radius_km", 2.0)),
            status="active",
            valid_until=datetime.utcnow() + timedelta(hours=ttl_hours),
        )
        session = db()
        if session.query(Hazard).filter_by(hazard_id=hazard.hazard_id).first():
            raise ApiError("hazard_id already exists", status_code=409, code="conflict")
        session.add(hazard)
        session.commit()
        return jsonify(hazard.to_dict()), 201

    @app.get("/api/hazards")
    def list_hazards():
        q = db().query(Hazard).order_by(Hazard.id.desc()).filter_by(status=request.args.get("status", "active"))
        if request.args.get("type"):
            q = q.filter_by(hazard_type=request.args["type"])
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(hazards=[h.to_dict() for h in items], pagination=meta)

    @app.delete("/api/hazards/<hazard_id>")
    def resolve_hazard(hazard_id):
        session = db()
        hazard = session.query(Hazard).filter_by(hazard_id=hazard_id).first()
        if hazard is None:
            raise ApiError("hazard not found", status_code=404, code="not_found")
        hazard.status = "resolved"
        session.commit()
        return jsonify(hazard.to_dict())

    # --- constraints -----------------------------------------------------------
    @app.post("/api/constraints")
    def add_constraint():
        data = get_json()
        require(data, "farm_id", "vehicle_type")
        constraint = TransportConstraint(
            farm_id=data["farm_id"],
            vehicle_type=data["vehicle_type"],
            constraints=data.get("constraints", {}),
        )
        session = db()
        session.add(constraint)
        session.commit()
        return jsonify(constraint.to_dict()), 201

    @app.get("/api/constraints")
    def list_constraints():
        q = db().query(TransportConstraint).order_by(TransportConstraint.id.desc())
        if request.args.get("farm_id"):
            q = q.filter_by(farm_id=request.args["farm_id"])
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(constraints=[c.to_dict() for c in items], pagination=meta)

    return app


def main():
    settings = Settings.from_env("traas", default_port=5002)
    app = create_app(settings)
    app.run(host="0.0.0.0", port=settings.port, debug=False)


if __name__ == "__main__":
    main()
