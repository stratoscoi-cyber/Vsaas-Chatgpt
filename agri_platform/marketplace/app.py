"""LGaaS — Logistics as a Service (customer-facing brand: prisaMove).

Flask application factory for the logistics marketplace: shippers post loads;
transporters / vehicle owners / logistics managers bid; a pricing engine
recommends fair rates and an AI-driven, culturally-aware haggling engine
proposes counter-offers and messages.
"""

from __future__ import annotations

import uuid
from typing import Optional

from flask import Flask, jsonify, request
from sqlalchemy import text

from ..common import db as db_helpers
from ..common.config import Settings
from ..common.errors import ApiError, as_float, get_json, register_error_handlers, require
from ..common.localization import current_lang, install_localization, register_localization
from ..common.logging import configure_logging, install_request_logging
from ..common.pagination import page_params, paginate
from ..common.ratelimit import RateLimiter, install_rate_limiting
from ..common.security import install_admin_auth, install_auth
from ..common import regions
from ..common import tax as tax_engine
from . import negotiation, service, tax_service
from .models import Base, Load, NegotiationMessage, Offer, TaxRule, Vehicle

SERVICE_NAME = "lgaas"
BRAND = "prisaMove"


def _new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def create_app(
    settings: Optional[Settings] = None,
    session_factory=None,
    composer: Optional[negotiation.MessageComposer] = None,
) -> Flask:
    settings = settings or Settings.from_env(SERVICE_NAME, default_port=5003)
    configure_logging(settings.log_level, settings.log_json)

    app = Flask(__name__)
    app.config["SETTINGS"] = settings

    if session_factory is None:
        engine, session_factory = db_helpers.make_session_factory(settings.database_url)
        db_helpers.init_models(engine, Base)
    app.config["SESSION_FACTORY"] = session_factory
    app.config["COMPOSER"] = composer or negotiation.make_composer(settings.haggle_ai_endpoint)

    install_request_logging(app, SERVICE_NAME)
    install_auth(app, settings.api_keys, settings.auth_enabled)
    install_rate_limiting(app, RateLimiter(settings.rate_limit_per_minute))
    install_admin_auth(app, settings.admin_api_keys)
    install_localization(app, settings.default_language)
    register_localization(app)
    register_error_handlers(app)
    app.config["DEFAULT_REGION_CODE"] = settings.default_region

    @app.teardown_appcontext
    def _cleanup(exc=None):  # noqa: ANN001
        if hasattr(session_factory, "remove"):
            session_factory.remove()

    @app.after_request
    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        return resp

    def db():
        return app.config["SESSION_FACTORY"]()

    def _load_or_404(session, ref) -> Load:
        load = session.query(Load).filter_by(ref=ref).first()
        if load is None:
            raise ApiError("load not found", status_code=404, code="not_found")
        return load

    def _offer_or_404(session, offer_id) -> Offer:
        offer = session.query(Offer).filter_by(id=offer_id).first()
        if offer is None:
            raise ApiError("offer not found", status_code=404, code="not_found")
        return offer

    # --- health ----------------------------------------------------------------
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

    # --- loads -----------------------------------------------------------------
    def _auto_currency(data) -> str:
        """Explicit currency wins; otherwise infer from the origin's region."""
        if data.get("currency"):
            return data["currency"]
        region = regions.region_for_coords(data.get("origin"))
        if region is None:
            region = regions.lookup(app.config["DEFAULT_REGION_CODE"]) or regions.DEFAULT_REGION
        return region.currency

    @app.post("/api/loads")
    def create_load():
        data = get_json()
        require(data, "shipper_id", "weight_kg")
        currency = _auto_currency(data)
        region = regions.region_for_coords(data.get("origin"))
        load = Load(
            ref=data.get("ref") or _new_ref("load"),
            shipper_id=data["shipper_id"],
            title=data.get("title"),
            origin=data.get("origin"),
            destination=data.get("destination"),
            weight_kg=float(data["weight_kg"]),
            dimensions_cm=data.get("dimensions_cm"),
            quantity=int(data.get("quantity", 1)),
            load_type=data.get("load_type", "general"),
            classifications=data.get("classifications", []),
            pickup_window=data.get("pickup_window"),
            delivery_deadline=data.get("delivery_deadline"),
            budget=data.get("budget"),
            currency=currency,
            distance_km=data.get("distance_km"),
            notes=data.get("notes"),
            status="open",
        )
        session = db()
        if session.query(Load).filter_by(ref=load.ref).first():
            raise ApiError("load ref already exists", status_code=409, code="conflict")
        session.add(load)
        session.commit()
        body = load.to_dict()
        if region is not None:
            body["region"] = region.to_dict()
        return jsonify(body), 201

    @app.get("/api/loads")
    def list_loads():
        q = db().query(Load).order_by(Load.id.desc())
        for field in ("status", "load_type", "shipper_id"):
            if request.args.get(field):
                q = q.filter_by(**{field: request.args[field]})
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(loads=[x.to_dict() for x in items], pagination=meta)

    @app.get("/api/loads/<ref>")
    def get_load(ref):
        return jsonify(_load_or_404(db(), ref).to_dict())

    @app.post("/api/loads/<ref>/status")
    def update_load_status(ref):
        data = get_json()
        require(data, "status")
        valid = {"open", "negotiating", "awarded", "in_transit", "delivered", "cancelled"}
        if data["status"] not in valid:
            raise ApiError("invalid status", status_code=422, code="validation_error", details={"valid": sorted(valid)})
        session = db()
        load = _load_or_404(session, ref)
        load.status = data["status"]
        session.commit()
        return jsonify(load.to_dict())

    def _region_code_for(coords) -> str:
        region = regions.region_for_coords(coords)
        if region is not None:
            return region.code
        return app.config["DEFAULT_REGION_CODE"]

    @app.post("/api/loads/<ref>/estimate")
    def estimate_load(ref):
        data = get_json()
        session = db()
        load = _load_or_404(session, ref)
        est = service.estimate_for_load(
            load,
            urgency_factor=float(data.get("urgency_factor", 1.0)),
            distance_km=data.get("distance_km"),
        )
        tax = tax_service.compute_for_region(
            session,
            base_amount=est.recommended,
            region_code=_region_code_for(load.origin),
            category=data.get("category", "transport_service"),
            currency=load.currency or "USD",
        )
        return jsonify(load_ref=ref, estimate=est.to_dict(), tax=tax)

    @app.post("/api/estimate")
    def estimate_adhoc():
        """Quote a hypothetical load without persisting it."""
        from . import pricing

        data = get_json()
        require(data, "weight_kg")
        currency = _auto_currency(data)
        region = regions.region_for_coords(data.get("origin"))
        est = pricing.estimate_rate(
            weight_kg=as_float(data, "weight_kg"),
            dimensions_cm=data.get("dimensions_cm"),
            quantity=int(data.get("quantity", 1)),
            load_type=data.get("load_type", "general"),
            classifications=data.get("classifications", []),
            origin=data.get("origin"),
            destination=data.get("destination"),
            distance_km=data.get("distance_km"),
            urgency_factor=float(data.get("urgency_factor", 1.0)),
            currency=currency,
        )
        tax = tax_service.compute_for_region(
            db(),
            base_amount=est.recommended,
            region_code=region.code if region else app.config["DEFAULT_REGION_CODE"],
            category=data.get("category", "transport_service"),
            currency=currency,
        )
        return jsonify(estimate=est.to_dict(), tax=tax, region=region.to_dict() if region else None)

    # --- vehicles --------------------------------------------------------------
    @app.post("/api/vehicles")
    def register_vehicle():
        data = get_json()
        require(data, "vehicle_id", "owner_id", "vehicle_type")
        session = db()
        if session.query(Vehicle).filter_by(vehicle_id=data["vehicle_id"]).first():
            raise ApiError("vehicle already exists", status_code=409, code="conflict")
        vehicle = Vehicle(
            vehicle_id=data["vehicle_id"],
            owner_id=data["owner_id"],
            vehicle_type=data["vehicle_type"],
            capacity_kg=data.get("capacity_kg"),
            capacity_m3=data.get("capacity_m3"),
            features=data.get("features", []),
            base_rate_per_km=data.get("base_rate_per_km"),
            location=data.get("location"),
            available=str(data.get("available", "true")).lower(),
        )
        session.add(vehicle)
        session.commit()
        return jsonify(vehicle.to_dict()), 201

    @app.get("/api/vehicles")
    def list_vehicles():
        q = db().query(Vehicle).order_by(Vehicle.id.desc())
        for field in ("owner_id", "vehicle_type"):
            if request.args.get(field):
                q = q.filter_by(**{field: request.args[field]})
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(vehicles=[x.to_dict() for x in items], pagination=meta)

    # --- offers / bids ---------------------------------------------------------
    @app.post("/api/loads/<ref>/offers")
    def place_offer(ref):
        data = get_json()
        require(data, "bidder_id", "bidder_role", "price")
        session = db()
        load = _load_or_404(session, ref)
        try:
            offer = service.place_offer(
                session, load,
                bidder_id=data["bidder_id"],
                bidder_role=data["bidder_role"],
                price=as_float(data, "price"),
                vehicle_id=data.get("vehicle_id"),
                eta_hours=data.get("eta_hours"),
                message=data.get("message"),
                negotiation_style=data.get("negotiation_style"),
            )
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(offer.to_dict()), 201

    @app.get("/api/loads/<ref>/offers")
    def list_offers(ref):
        _load_or_404(db(), ref)
        q = db().query(Offer).filter_by(load_ref=ref).order_by(Offer.price.asc())
        if request.args.get("status"):
            q = q.filter_by(status=request.args["status"])
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(offers=[o.to_dict() for o in items], pagination=meta)

    @app.get("/api/offers/<int:offer_id>")
    def get_offer(offer_id):
        return jsonify(_offer_or_404(db(), offer_id).to_dict())

    @app.post("/api/offers/<int:offer_id>/counter")
    def counter(offer_id):
        data = get_json()
        require(data, "price", "author_id", "author_role")
        if data["author_role"] not in ("shipper", "bidder"):
            raise ApiError("author_role must be 'shipper' or 'bidder'", status_code=422, code="validation_error")
        session = db()
        offer = _offer_or_404(session, offer_id)
        try:
            offer = service.counter_offer(
                session, offer,
                price=as_float(data, "price"),
                author_id=data["author_id"],
                author_role=data["author_role"],
                message=data.get("message"),
                negotiation_style=data.get("negotiation_style"),
            )
        except ValueError as exc:
            raise ApiError(str(exc), status_code=409, code="conflict")
        return jsonify(offer.to_dict())

    @app.post("/api/offers/<int:offer_id>/accept")
    def accept(offer_id):
        session = db()
        offer = _offer_or_404(session, offer_id)
        load = _load_or_404(session, offer.load_ref)
        try:
            offer = service.accept_offer(session, load, offer)
        except ValueError as exc:
            raise ApiError(str(exc), status_code=409, code="conflict")
        return jsonify(offer=offer.to_dict(), load=load.to_dict())

    @app.post("/api/offers/<int:offer_id>/reject")
    def reject(offer_id):
        session = db()
        offer = service.reject_offer(session, _offer_or_404(session, offer_id))
        return jsonify(offer.to_dict())

    @app.post("/api/offers/<int:offer_id>/haggle")
    def haggle(offer_id):
        """AI-driven, culturally-aware counter-offer suggestion."""
        data = get_json()
        require(data, "as_role")
        session = db()
        offer = _offer_or_404(session, offer_id)
        load = _load_or_404(session, offer.load_ref)
        # Language: explicit > request locale (?lang / Accept-Language) > load region.
        region = regions.region_for_coords(load.origin)
        lang = data.get("lang") or current_lang()
        if not data.get("lang") and region is not None:
            from ..common import i18n
            lang = i18n.resolve_language(region_language=region.primary_language, default=current_lang())
        try:
            result = service.haggle(
                session, load, offer,
                as_role=data["as_role"],
                target_price=data.get("target_price"),
                style=data.get("negotiation_style"),
                custom_profile=data.get("custom_profile"),
                composer=app.config["COMPOSER"],
                counterparty_name=data.get("counterparty_name"),
                lang=lang,
                record=bool(data.get("record", True)),
            )
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(result)

    # --- negotiation thread & styles ------------------------------------------
    @app.get("/api/loads/<ref>/messages")
    def messages(ref):
        _load_or_404(db(), ref)
        q = db().query(NegotiationMessage).filter_by(load_ref=ref).order_by(NegotiationMessage.id.asc())
        if request.args.get("offer_id"):
            q = q.filter_by(offer_id=int(request.args["offer_id"]))
        return jsonify(messages=[m.to_dict() for m in q.all()])

    @app.get("/api/negotiation-styles")
    def styles():
        return jsonify(styles=[p.to_dict() for p in negotiation.PROFILES.values()],
                       default=negotiation.DEFAULT_PROFILE_KEY)

    # --- tax (public quote) ----------------------------------------------------
    @app.post("/api/tax/quote")
    def tax_quote():
        """Compute taxes for a base amount using the operator's configured rules.

        Returns zero tax with ``configured=false`` when no rules apply — never a
        fabricated rate.
        """
        data = get_json()
        base = as_float(data, "base_amount")
        region_code = data.get("region_code")
        if not region_code:
            region = regions.region_for_coords(data.get("origin"))
            region_code = region.code if region else app.config["DEFAULT_REGION_CODE"]
        result = tax_service.compute_for_region(
            db(),
            base_amount=base,
            region_code=region_code,
            category=data.get("category", "transport_service"),
            currency=data.get("currency", "USD"),
            on_date=_parse_iso_date(data.get("on_date")),
        )
        return jsonify(result)

    # --- tax rules (admin only) ------------------------------------------------
    def _validate_rule(data, *, partial=False):
        if not partial:
            require(data, "code", "region_code", "collection", "rate_percent")
        if "collection" in data and data["collection"] not in tax_engine.VALID_COLLECTIONS:
            raise ApiError("collection must be 'add' or 'withhold'", status_code=422,
                           code="validation_error", details={"valid": sorted(tax_engine.VALID_COLLECTIONS)})
        if "basis" in data and data["basis"] not in tax_engine.VALID_BASES:
            raise ApiError("basis must be 'net' or 'compound'", status_code=422,
                           code="validation_error", details={"valid": sorted(tax_engine.VALID_BASES)})

    @app.post("/api/admin/tax-rules")
    def create_tax_rule():
        data = get_json()
        _validate_rule(data)
        session = db()
        if session.query(TaxRule).filter_by(code=data["code"]).first():
            raise ApiError("tax rule code already exists", status_code=409, code="conflict")
        rule = TaxRule(
            code=data["code"],
            region_code=data["region_code"],
            name=data.get("name", data["code"]),
            tax_type=data.get("tax_type", "tax"),
            collection=data["collection"],
            rate_percent=float(data["rate_percent"]),
            basis=data.get("basis", "net"),
            applies_to=data.get("applies_to", []),
            threshold_min=float(data.get("threshold_min", 0.0)),
            sequence=int(data.get("sequence", 0)),
            effective_from=data.get("effective_from"),
            effective_to=data.get("effective_to"),
            statutory_reference=data.get("statutory_reference"),
            active=bool(data.get("active", True)),
        )
        session.add(rule)
        session.commit()
        return jsonify(rule.to_dict()), 201

    @app.get("/api/admin/tax-rules")
    def list_tax_rules():
        q = db().query(TaxRule).order_by(TaxRule.region_code, TaxRule.sequence, TaxRule.code)
        if request.args.get("region_code"):
            q = q.filter_by(region_code=request.args["region_code"])
        if request.args.get("active") is not None:
            q = q.filter_by(active=request.args["active"].lower() == "true")
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(tax_rules=[r.to_dict() for r in items], pagination=meta)

    @app.get("/api/admin/tax-rules/<code>")
    def get_tax_rule(code):
        rule = db().query(TaxRule).filter_by(code=code).first()
        if rule is None:
            raise ApiError("tax rule not found", status_code=404, code="not_found")
        return jsonify(rule.to_dict())

    @app.put("/api/admin/tax-rules/<code>")
    def update_tax_rule(code):
        data = get_json()
        _validate_rule(data, partial=True)
        session = db()
        rule = session.query(TaxRule).filter_by(code=code).first()
        if rule is None:
            raise ApiError("tax rule not found", status_code=404, code="not_found")
        for field in ("region_code", "name", "tax_type", "collection", "rate_percent", "basis",
                      "applies_to", "threshold_min", "sequence", "effective_from", "effective_to",
                      "statutory_reference", "active"):
            if field in data:
                setattr(rule, field, data[field])
        from datetime import datetime as _dt
        rule.updated_at = _dt.utcnow()
        session.commit()
        return jsonify(rule.to_dict())

    @app.delete("/api/admin/tax-rules/<code>")
    def delete_tax_rule(code):
        session = db()
        rule = session.query(TaxRule).filter_by(code=code).first()
        if rule is None:
            raise ApiError("tax rule not found", status_code=404, code="not_found")
        # Soft-disable by default (preserves audit history); ?hard=true to remove.
        if request.args.get("hard", "").lower() == "true":
            session.delete(rule)
            session.commit()
            return jsonify(deleted=code, hard=True)
        rule.active = False
        session.commit()
        return jsonify(rule.to_dict())

    return app


def _parse_iso_date(value):
    from datetime import date as _date
    if not value:
        return None
    try:
        return _date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ApiError("on_date must be ISO format YYYY-MM-DD", status_code=422, code="validation_error")


def main():
    settings = Settings.from_env(SERVICE_NAME, default_port=5003)
    app = create_app(settings)
    app.run(host="0.0.0.0", port=settings.port, debug=False)


if __name__ == "__main__":
    main()
