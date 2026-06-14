"""WFAAS Flask application factory.

Wires the weather/agronomy/drone domain to the shared enterprise infrastructure
(config, structured logging, API-key auth, rate limiting, caching, consistent
error envelopes, notifications and background monitoring). Every collaborator is
injectable so the full HTTP surface is testable without external services.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Optional

from flask import Flask, g, jsonify, request
from sqlalchemy import text

from ..common import db as db_helpers
from ..common.cache import Cache, make_cache
from ..common.config import Settings
from ..common.errors import ApiError, as_float, get_json, register_error_handlers, require
from ..common.geo import buffer_point_ring, calculate_gdd
from ..common.localization import install_localization, register_localization
from ..common.logging import configure_logging, install_request_logging
from ..common import regions
from ..common.pagination import page_params, paginate
from ..common.ratelimit import RateLimiter, install_rate_limiting
from ..common.security import install_auth
from ..common.weather import OpenMeteoClient, WeatherClient, WeatherError
from . import service
from .ai import InferenceClient, InferenceError, make_inference_client
from .models import AlertPreference, Base, Drone, DroneMission, Farm, WeatherAlert
from .notifications import ConsoleNotifier, Notifier, dispatch_alert, make_notifier

SERVICE_NAME = "wfaas"
BRAND = "prisaForecast"


def _today() -> str:
    return date.today().isoformat()


def _cache_key(*parts) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return "wfaas:" + hashlib.sha256(raw.encode()).hexdigest()


def create_app(
    settings: Optional[Settings] = None,
    session_factory=None,
    weather_client: Optional[WeatherClient] = None,
    notifier: Optional[Notifier] = None,
    inference_client: Optional[InferenceClient] = None,
    cache: Optional[Cache] = None,
) -> Flask:
    settings = settings or Settings.from_env("wfaas", default_port=5001)
    configure_logging(settings.log_level, settings.log_json)

    app = Flask(__name__)
    app.config["SETTINGS"] = settings

    if session_factory is None:
        engine, session_factory = db_helpers.make_session_factory(settings.database_url)
        db_helpers.init_models(engine, Base)
    app.config["SESSION_FACTORY"] = session_factory
    app.config["WEATHER_CLIENT"] = weather_client or OpenMeteoClient(settings.open_meteo_url)
    app.config["NOTIFIER"] = notifier or make_notifier(settings)
    app.config["INFERENCE"] = inference_client or make_inference_client(settings.drone_ai_endpoint)
    app.config["CACHE"] = cache or make_cache(settings.redis_url, settings.cache_ttl_seconds)

    # Cross-cutting middleware.
    install_request_logging(app, SERVICE_NAME)
    install_auth(app, settings.api_keys, settings.auth_enabled)
    install_rate_limiting(app, RateLimiter(settings.rate_limit_per_minute))
    install_localization(app, settings.default_language)
    register_localization(app)
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

    def wx() -> WeatherClient:
        return app.config["WEATHER_CLIENT"]

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

    # --- farms -----------------------------------------------------------------
    @app.post("/api/farms")
    def register_farm():
        data = get_json()
        require(data, "farm_id")
        session = db()
        if session.query(Farm).filter_by(farm_id=data["farm_id"]).first():
            raise ApiError("farm already exists", status_code=409, code="conflict")
        farm = Farm(
            farm_id=data["farm_id"],
            name=data.get("name"),
            coordinates=data.get("coordinates"),
            owner=data.get("owner"),
            owner_email=data.get("owner_email"),
            drone_fleet=[],
            monitoring_schedule={},
        )
        session.add(farm)
        session.commit()
        return jsonify(farm.to_dict()), 201

    @app.get("/api/farms")
    def list_farms():
        page, size = page_params()
        items, meta = paginate(db().query(Farm).order_by(Farm.id.desc()), page, size)
        return jsonify(farms=[f.to_dict() for f in items], pagination=meta)

    @app.get("/api/farms/<farm_id>")
    def get_farm(farm_id):
        farm = db().query(Farm).filter_by(farm_id=farm_id).first()
        if farm is None:
            raise ApiError("farm not found", status_code=404, code="not_found")
        return jsonify(farm.to_dict())

    @app.post("/api/farms/<farm_id>/drones")
    def attach_drone(farm_id):
        data = get_json()
        require(data, "drone_id")
        session = db()
        farm = session.query(Farm).filter_by(farm_id=farm_id).first()
        if farm is None:
            raise ApiError("farm not found", status_code=404, code="not_found")
        drone = session.query(Drone).filter_by(drone_id=data["drone_id"]).first()
        if drone is None:
            raise ApiError("drone not found", status_code=404, code="not_found")
        fleet = list(farm.drone_fleet or [])
        if data["drone_id"] not in fleet:
            fleet.append(data["drone_id"])
        farm.drone_fleet = fleet
        drone.assigned_farm = farm_id
        session.commit()
        return jsonify(farm.to_dict())

    # --- drones ----------------------------------------------------------------
    @app.post("/api/drones")
    def create_drone():
        data = get_json()
        require(data, "drone_id")
        session = db()
        if session.query(Drone).filter_by(drone_id=data["drone_id"]).first():
            raise ApiError("drone already exists", status_code=409, code="conflict")
        drone = Drone(
            drone_id=data["drone_id"],
            model=data.get("model", "Generic Drone"),
            capabilities=data.get("capabilities", ["camera", "gps"]),
            battery_level=float(data.get("battery_level", 100.0)),
            status="available",
        )
        session.add(drone)
        session.commit()
        return jsonify(drone.to_dict()), 201

    @app.get("/api/drones")
    def list_drones():
        q = db().query(Drone).order_by(Drone.id.desc())
        if request.args.get("status"):
            q = q.filter_by(status=request.args["status"])
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(drones=[d.to_dict() for d in items], pagination=meta)

    @app.post("/api/drones/missions")
    def schedule_mission():
        data = get_json()
        require(data, "farm_id", "mission_type", "waypoints")
        try:
            mission = service.schedule_drone_mission(
                db(),
                farm_id=data["farm_id"],
                mission_type=data["mission_type"],
                waypoints=data["waypoints"],
                payload=data.get("payload"),
                drone_id=data.get("drone_id"),
            )
        except LookupError as exc:
            raise ApiError(str(exc), status_code=409, code="no_drone_available")
        return jsonify(mission.to_dict()), 201

    @app.get("/api/drones/<drone_id>/missions")
    def drone_missions(drone_id):
        q = db().query(DroneMission).filter_by(drone_id=drone_id).order_by(DroneMission.id.desc())
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(missions=[m.to_dict() for m in items], pagination=meta)

    @app.put("/api/missions/<mission_id>/result")
    def complete_mission(mission_id):
        data = get_json()
        try:
            mission = service.complete_drone_mission(db(), mission_id, data.get("results", {}))
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="not_found")
        return jsonify(mission.to_dict())

    @app.post("/api/missions/<mission_id>/analyze")
    def analyze_mission(mission_id):
        """Run external AI inference over a mission's captured imagery."""
        data = get_json()
        session = db()
        mission = session.query(DroneMission).filter_by(mission_id=mission_id).first()
        if mission is None:
            raise ApiError("mission not found", status_code=404, code="not_found")
        image_refs = data.get("images", [])
        try:
            results = app.config["INFERENCE"].analyze(mission_id, image_refs, {"farm_id": mission.farm_id})
        except InferenceError as exc:
            raise ApiError(str(exc), status_code=503, code="inference_unavailable")
        mission = service.complete_drone_mission(session, mission_id, results)
        return jsonify(mission.to_dict())

    # --- weather & agronomy ----------------------------------------------------
    def _cached_hourly(lat, lon, variables, start, end):
        key = _cache_key("hourly", lat, lon, sorted(variables), start, end)
        cached = app.config["CACHE"].get(key)
        if cached is not None:
            return cached
        payload = wx().hourly(lat, lon, variables, start, end)
        app.config["CACHE"].set(key, payload)
        return payload

    def _cached_daily(lat, lon, variables, start, end):
        key = _cache_key("daily", lat, lon, sorted(variables), start, end)
        cached = app.config["CACHE"].get(key)
        if cached is not None:
            return cached
        payload = wx().daily(lat, lon, variables, start, end)
        app.config["CACHE"].set(key, payload)
        return payload

    @app.post("/api/weather/forecast")
    def weather_forecast():
        data = get_json()
        lat, lon = as_float(data, "lat"), as_float(data, "lon")
        variables = data.get("variables", ["temperature_2m", "precipitation", "soil_moisture_1_to_3cm"])
        try:
            payload = _cached_hourly(lat, lon, variables, data.get("start_date", _today()), data.get("end_date", _today()))
        except WeatherError as exc:
            raise ApiError(str(exc), status_code=502, code="upstream_error")
        return jsonify(status="success", data=payload)

    @app.post("/api/weather/daily")
    def weather_daily():
        data = get_json()
        lat, lon = as_float(data, "lat"), as_float(data, "lon")
        variables = data.get("variables", ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"])
        try:
            payload = _cached_daily(lat, lon, variables, data.get("start_date", _today()), data.get("end_date", _today()))
        except WeatherError as exc:
            raise ApiError(str(exc), status_code=502, code="upstream_error")
        return jsonify(status="success", data=payload)

    @app.post("/api/agronomy/gdd")
    def gdd():
        data = get_json()
        lat, lon = as_float(data, "lat"), as_float(data, "lon")
        try:
            payload = _cached_daily(
                lat, lon,
                ["temperature_2m_max", "temperature_2m_min"],
                data.get("start_date", _today()),
                data.get("end_date", _today()),
            )
        except WeatherError as exc:
            raise ApiError(str(exc), status_code=502, code="upstream_error")
        daily = payload.get("daily", {})
        result = calculate_gdd(
            daily.get("temperature_2m_max", []),
            daily.get("temperature_2m_min", []),
            base_temp=float(data.get("base_temp", 10.0)),
            upper_temp=data.get("upper_temp"),
        )
        return jsonify(status="success", **result)

    # --- alerts ----------------------------------------------------------------
    @app.post("/api/alerts/detect")
    def detect_alert():
        data = get_json()
        require(data, "farm_id")
        lat, lon = as_float(data, "lat"), as_float(data, "lon")
        farm_id = data["farm_id"]
        alert_type = data.get("alert_type", service.ALERT_TYPES["WEATHER_HAZARD"])
        coords = {"lat": lat, "lon": lon}
        session = db()

        try:
            if alert_type == service.ALERT_TYPES["WEATHER_HAZARD"]:
                hourly = _cached_hourly(lat, lon, ["precipitation_probability", "wind_speed_10m"], _today(), _today()).get("hourly", {})
                spec = service.evaluate_weather_hazard(
                    hourly.get("precipitation_probability", []), hourly.get("wind_speed_10m", [])
                )
            elif alert_type == service.ALERT_TYPES["SOIL_CONDITION"]:
                hourly = _cached_hourly(lat, lon, ["soil_moisture_1_to_3cm"], _today(), _today()).get("hourly", {})
                spec = service.evaluate_soil_condition(hourly.get("soil_moisture_1_to_3cm", []))
            elif alert_type == service.ALERT_TYPES["DRONE_MONITORING"]:
                spec = None
            else:
                raise ApiError(f"unsupported alert_type '{alert_type}'", status_code=422, code="validation_error")
        except WeatherError as exc:
            raise ApiError(str(exc), status_code=502, code="upstream_error")

        if alert_type == service.ALERT_TYPES["DRONE_MONITORING"]:
            ring = buffer_point_ring(lat, lon, 0.2, segments=4)
            waypoints = [[c[1], c[0]] for c in ring]
            try:
                mission = service.schedule_drone_mission(
                    session, farm_id, "health_scan", waypoints,
                    payload={"sensors": ["NDVI", "multispectral", "thermal"]},
                )
            except LookupError as exc:
                raise ApiError(str(exc), status_code=409, code="no_drone_available")
            spec = {
                "alert_type": service.ALERT_TYPES["DRONE_MONITORING"],
                "severity": "Medium",
                "message": "Drone deployed for crop health monitoring",
                "related_data": {"mission_id": mission.mission_id, "drone_id": mission.drone_id},
            }

        if spec is None:
            return jsonify(message="no significant alert detected"), 200
        alert = service.create_alert(session, farm_id, spec, coords)
        notified = dispatch_alert(session, app.config["NOTIFIER"], alert)
        body = alert.to_dict()
        body["notified"] = notified
        region = regions.region_for_point(lat, lon)
        body["region"] = region.to_dict()
        return jsonify(body), 201

    @app.get("/api/alerts")
    def list_alerts():
        q = db().query(WeatherAlert).order_by(WeatherAlert.id.desc())
        if request.args.get("farm_id"):
            q = q.filter_by(farm_id=request.args["farm_id"])
        if request.args.get("type"):
            q = q.filter_by(alert_type=request.args["type"])
        if request.args.get("severity"):
            q = q.filter_by(severity=request.args["severity"])
        if request.args.get("resolved") is not None:
            q = q.filter_by(resolved=request.args["resolved"].lower() == "true")
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(alerts=[a.to_dict() for a in items], pagination=meta)

    @app.post("/api/alerts/<int:alert_id>/resolve")
    def resolve_alert(alert_id):
        session = db()
        alert = session.query(WeatherAlert).filter_by(id=alert_id).first()
        if alert is None:
            raise ApiError("alert not found", status_code=404, code="not_found")
        alert.resolved = True
        session.commit()
        return jsonify(alert.to_dict())

    @app.post("/api/alerts/preferences")
    def set_preferences():
        data = get_json()
        require(data, "farm_id")
        session = db()
        pref = session.query(AlertPreference).filter_by(farm_id=data["farm_id"]).first()
        if pref is None:
            pref = AlertPreference(farm_id=data["farm_id"])
            session.add(pref)
        for field in ("alert_types", "notification_frequency", "email_enabled", "sms_enabled"):
            if field in data:
                setattr(pref, field, data[field])
        session.commit()
        return jsonify(pref.to_dict())

    @app.get("/api/alerts/preferences/<farm_id>")
    def get_preferences(farm_id):
        pref = db().query(AlertPreference).filter_by(farm_id=farm_id).first()
        if pref is None:
            raise ApiError("preferences not found", status_code=404, code="not_found")
        return jsonify(pref.to_dict())

    # --- monitoring ------------------------------------------------------------
    @app.post("/api/monitoring/scan")
    def monitoring_scan():
        """Manually trigger a hazard scan across all registered farms."""
        from .monitoring import scan_farms

        alerts = scan_farms(db(), wx(), app.config["NOTIFIER"])
        return jsonify(scanned=True, alerts_created=len(alerts), alerts=[a.to_dict() for a in alerts])

    return app


def main():
    settings = Settings.from_env("wfaas", default_port=5001)
    app = create_app(settings)

    if settings.monitor_enabled:
        from .monitoring import MonitorScheduler, scan_farms

        factory = app.config["SESSION_FACTORY"]

        def _scan():
            session = factory()
            try:
                scan_farms(session, app.config["WEATHER_CLIENT"], app.config["NOTIFIER"])
            finally:
                if hasattr(factory, "remove"):
                    factory.remove()

        MonitorScheduler(_scan, settings.monitor_interval_minutes).start()

    app.run(host="0.0.0.0", port=settings.port, debug=False)


if __name__ == "__main__":
    main()
