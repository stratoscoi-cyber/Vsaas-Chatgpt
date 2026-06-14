"""LGaaS — Logistics as a Service (customer-facing brand: prisaMove).

Flask application factory for the logistics marketplace: shippers post loads;
transporters / vehicle owners / logistics managers bid; a pricing engine
recommends fair rates and an AI-driven, culturally-aware haggling engine
proposes counter-offers and messages.
"""

from __future__ import annotations

import json
import uuid
from queue import Empty
from typing import Optional

from flask import Flask, Response, jsonify, request, stream_with_context
from sqlalchemy import text

from ..common import db as db_helpers
from ..common.config import Settings
from ..common.errors import ApiError, as_float, get_json, register_error_handlers, require
from ..common.localization import current_lang, install_localization, register_localization
from ..common.logging import configure_logging, install_request_logging
from ..common.pagination import page_params, paginate
from ..common.ratelimit import RateLimiter, install_rate_limiting
from ..common.security import install_admin_auth, install_auth
from ..common import currency as currency_mod
from ..common import regions
from ..common import tax as tax_engine
import hashlib

from ..common.eventbus import EventBus
from ..common.holiday_provider import make_holiday_provider
from . import (
    calendar_service, compliance_service, loads_planning, negotiation, service,
    tax_service, tracking,
)
from .routing_client import make_traas_client
from .models import (
    ApprovedAgency, Base, ComplianceDecision, ComplianceDocument, Consolidation,
    DelayReason, Driver, Holiday, Inspection, Load, NegotiationMessage, Offer,
    RegionComplianceRule, ServiceCenter, Shipment, ShipmentEvent, TaxRule,
    TrackPoint, Vehicle,
)

SERVICE_NAME = "lgaas"
BRAND = "prisaMove"


def _new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def create_app(
    settings: Optional[Settings] = None,
    session_factory=None,
    composer: Optional[negotiation.MessageComposer] = None,
    holiday_provider=None,
    tracking_notifier=None,
    traas_client=None,
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
    app.config["HOLIDAY_PROVIDER"] = holiday_provider or make_holiday_provider(settings.holiday_provider)
    app.config["TRAAS_CLIENT"] = traas_client or make_traas_client(settings.traas_url)
    # Event bus backs server-sent events; every shipment event also reaches the
    # configured webhook/console notifier via a composite.
    bus = EventBus()
    app.config["EVENT_BUS"] = bus
    base_notifier = tracking_notifier or tracking.make_tracking_notifier(settings)
    app.config["TRACKING_NOTIFIER"] = tracking.CompositeNotifier([tracking.BusTrackingNotifier(bus), base_notifier])
    app.config["COMPLIANCE_ENFORCED"] = settings.compliance_enforced

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
            load_mode=data.get("load_mode", "ftl"),
            insurance_opted=bool(data.get("insurance_opted", False)),
            insurance_level=data.get("insurance_level"),
            insurance_value=data.get("insurance_value"),
            notes=data.get("notes"),
            status="open",
        )
        if load.load_mode not in ("ftl", "ltl", "consolidation"):
            raise ApiError("load_mode must be ftl, ltl or consolidation", status_code=422, code="validation_error")
        # Derive regions, distance and scope.
        plan = loads_planning.classify_load(load)
        load.origin_region = plan["origin_region"]
        load.destination_region = plan["destination_region"]
        load.distance_km = load.distance_km if load.distance_km is not None else plan["distance_km"]
        load.scope = plan["scope"]
        session = db()
        if session.query(Load).filter_by(ref=load.ref).first():
            raise ApiError("load ref already exists", status_code=409, code="conflict")
        session.add(load)
        session.commit()
        body = load.to_dict()
        if region is not None:
            body["region"] = region.to_dict()
        deadline = _parse_iso_date(load.delivery_deadline) if load.delivery_deadline else None
        if deadline is not None:
            body["schedule"] = calendar_service.schedule_info(
                session, region.code if region else app.config["DEFAULT_REGION_CODE"], deadline,
                provider=app.config["HOLIDAY_PROVIDER"])
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

    def _money(amount, code) -> str:
        return currency_mod.format_amount(amount, code, current_lang())

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
            currency_code=load.currency or "USD",
            locale=current_lang(),
        )
        est_dict = est.to_dict()
        est_dict["recommended_formatted"] = _money(est.recommended, load.currency or "USD")
        return jsonify(load_ref=ref, estimate=est_dict, tax=tax)

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
            currency_code=currency,
            locale=current_lang(),
        )
        est_dict = est.to_dict()
        est_dict["recommended_formatted"] = _money(est.recommended, currency)
        return jsonify(estimate=est_dict, tax=tax, region=region.to_dict() if region else None)

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
            region_code=data.get("region_code"),
            driver_id=data.get("driver_id"),
            tracker_serial=data.get("tracker_serial"),
            tracker_model=data.get("tracker_model"),
            tracker_approved=bool(data.get("tracker_approved", False)),
            tracker_serviceable=bool(data.get("tracker_serviceable", False)),
            camera_serial=data.get("camera_serial"),
            compliance_status="pending",
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
        if app.config["COMPLIANCE_ENFORCED"]:
            blocking = compliance_service.offer_eligibility(session, data.get("vehicle_id"))
            if blocking:
                raise ApiError("vehicle/driver not eligible", status_code=422,
                               code="compliance_blocked", details={"reasons": blocking})
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
        data = get_json()
        session = db()
        offer = _offer_or_404(session, offer_id)
        load = _load_or_404(session, offer.load_ref)
        try:
            offer = service.accept_offer(session, load, offer)
        except ValueError as exc:
            raise ApiError(str(exc), status_code=409, code="conflict")
        # Awarding a load opens a live, trackable shipment.
        shipment = None
        if load.origin and load.destination:
            shipment = tracking.create_shipment(
                session, load, offer,
                driver_name=data.get("driver_name"),
                driver_phone=data.get("driver_phone"),
                avg_speed_kmh=data.get("avg_speed_kmh"),
                notifier=app.config["TRACKING_NOTIFIER"],
            )
        return jsonify(offer=offer.to_dict(), load=load.to_dict(),
                       shipment=shipment.to_dict() if shipment else None)

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
            currency_code=data.get("currency", "USD"),
            locale=current_lang(),
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

    # --- region cultural profile ----------------------------------------------
    @app.get("/api/regions/<code>/profile")
    def region_profile(code):
        region = regions.lookup(code)
        if region is None:
            raise ApiError("region not found", status_code=404, code="not_found")
        from datetime import date as _date
        cur = currency_mod.get_currency(region.currency)
        return jsonify(
            region=region.to_dict(),
            currency=cur.to_dict(),
            sample_amount=currency_mod.format_amount(1234567.5, region.currency, current_lang()),
            default_negotiation_style=region.default_negotiation_style,
            negotiation_profile=negotiation.get_profile(region.default_negotiation_style).to_dict(),
            weekend=list(region.weekend),
            upcoming_holidays=calendar_service.upcoming(db(), code, _date.today(),
                                                        provider=app.config["HOLIDAY_PROVIDER"]),
        )

    # --- public holidays (admin) ----------------------------------------------
    @app.post("/api/admin/holidays")
    def create_holiday():
        data = get_json()
        require(data, "code", "region_code", "name", "recurrence")
        if data["recurrence"] not in ("fixed", "date"):
            raise ApiError("recurrence must be 'fixed' or 'date'", status_code=422, code="validation_error")
        if data["recurrence"] == "fixed" and not (data.get("month") and data.get("day")):
            raise ApiError("fixed holidays require month and day", status_code=422, code="validation_error")
        if data["recurrence"] == "date" and not data.get("date"):
            raise ApiError("one-off holidays require a date", status_code=422, code="validation_error")
        session = db()
        if session.query(Holiday).filter_by(code=data["code"]).first():
            raise ApiError("holiday code already exists", status_code=409, code="conflict")
        holiday = Holiday(
            code=data["code"], region_code=data["region_code"], name=data["name"],
            recurrence=data["recurrence"], month=data.get("month"), day=data.get("day"),
            date=data.get("date"), note=data.get("note"), active=bool(data.get("active", True)),
        )
        session.add(holiday)
        session.commit()
        return jsonify(holiday.to_dict()), 201

    @app.get("/api/admin/holidays")
    def list_admin_holidays():
        q = db().query(Holiday).order_by(Holiday.region_code, Holiday.code)
        if request.args.get("region_code"):
            q = q.filter_by(region_code=request.args["region_code"])
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(holidays=[h.to_dict() for h in items], pagination=meta)

    @app.put("/api/admin/holidays/<code>")
    def update_holiday(code):
        data = get_json()
        session = db()
        holiday = session.query(Holiday).filter_by(code=code).first()
        if holiday is None:
            raise ApiError("holiday not found", status_code=404, code="not_found")
        for field in ("region_code", "name", "recurrence", "month", "day", "date", "note", "active"):
            if field in data:
                setattr(holiday, field, data[field])
        from datetime import datetime as _dt
        holiday.updated_at = _dt.utcnow()
        session.commit()
        return jsonify(holiday.to_dict())

    @app.delete("/api/admin/holidays/<code>")
    def delete_holiday(code):
        session = db()
        holiday = session.query(Holiday).filter_by(code=code).first()
        if holiday is None:
            raise ApiError("holiday not found", status_code=404, code="not_found")
        if request.args.get("hard", "").lower() == "true":
            session.delete(holiday)
            session.commit()
            return jsonify(deleted=code, hard=True)
        holiday.active = False
        session.commit()
        return jsonify(holiday.to_dict())

    # --- shipment tracking -----------------------------------------------------
    def _shipment_or_404(session, sid) -> Shipment:
        shipment = session.query(Shipment).filter_by(shipment_id=sid).first()
        if shipment is None:
            raise ApiError("shipment not found", status_code=404, code="not_found")
        return shipment

    @app.get("/api/shipments")
    def list_shipments():
        q = db().query(Shipment).order_by(Shipment.id.desc())
        for field in ("status", "load_ref", "carrier_id"):
            if request.args.get(field):
                q = q.filter_by(**{field: request.args[field]})
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(shipments=[s.to_dict() for s in items], pagination=meta)

    @app.get("/api/shipments/<sid>")
    def get_shipment(sid):
        return jsonify(_shipment_or_404(db(), sid).to_dict())

    @app.get("/api/loads/<ref>/shipment")
    def load_shipment(ref):
        shipment = db().query(Shipment).filter_by(load_ref=ref).order_by(Shipment.id.desc()).first()
        if shipment is None:
            raise ApiError("no shipment for load", status_code=404, code="not_found")
        return jsonify(shipment.to_dict())

    @app.post("/api/shipments/<sid>/location")
    def post_location(sid):
        data = get_json()
        lat, lon = as_float(data, "lat"), as_float(data, "lon")
        session = db()
        shipment = _shipment_or_404(session, sid)
        shipment = tracking.add_location(
            session, shipment, lat, lon,
            speed_kmh=data.get("speed_kmh"), heading=data.get("heading"),
            source=data.get("source", "driver"), notifier=app.config["TRACKING_NOTIFIER"],
        )
        return jsonify(shipment.to_dict())

    @app.get("/api/shipments/<sid>/track")
    def get_track(sid):
        _shipment_or_404(db(), sid)
        points = db().query(TrackPoint).filter_by(shipment_id=sid).order_by(TrackPoint.id.asc()).all()
        return jsonify(shipment_id=sid, track=[p.to_dict() for p in points])

    @app.post("/api/shipments/<sid>/status")
    def post_status(sid):
        data = get_json()
        require(data, "status")
        session = db()
        shipment = _shipment_or_404(session, sid)
        try:
            shipment = tracking.update_status(
                session, shipment, data["status"], message=data.get("message"),
                created_by=data.get("created_by"), notifier=app.config["TRACKING_NOTIFIER"])
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(shipment.to_dict())

    @app.post("/api/shipments/<sid>/delay")
    def post_delay(sid):
        data = get_json()
        session = db()
        shipment = _shipment_or_404(session, sid)
        shipment = tracking.report_delay(
            session, shipment, delay_minutes=as_float(data, "delay_minutes"),
            reason_code=data.get("reason_code"), message=data.get("message"),
            created_by=data.get("created_by"), notifier=app.config["TRACKING_NOTIFIER"])
        return jsonify(shipment.to_dict())

    @app.post("/api/shipments/<sid>/reroute")
    def post_reroute(sid):
        data = get_json()
        session = db()
        shipment = _shipment_or_404(session, sid)
        shipment = tracking.reroute(
            session, shipment, reason_code=data.get("reason_code"), message=data.get("message"),
            new_destination=data.get("new_destination"), new_distance_km=data.get("new_distance_km"),
            waypoints=data.get("waypoints"), hazard=data.get("hazard"),
            traas_client=app.config["TRAAS_CLIENT"], created_by=data.get("created_by"),
            notifier=app.config["TRACKING_NOTIFIER"])
        return jsonify(shipment.to_dict())

    @app.get("/api/shipments/<sid>/stream")
    def stream_shipment(sid):
        """Server-Sent Events stream of a shipment's live events (push)."""
        shipment = _shipment_or_404(db(), sid)
        snapshot = shipment.to_dict()
        bus = app.config["EVENT_BUS"]
        channel = f"shipment:{sid}"
        q = bus.subscribe(channel)

        @stream_with_context
        def gen():
            try:
                yield "retry: 3000\n\n"
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
                while True:
                    try:
                        event = q.get(timeout=15)
                        yield f"event: {event.get('event_type', 'message')}\ndata: {json.dumps(event)}\n\n"
                    except Empty:
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(channel, q)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                 "Connection": "keep-alive"})

    @app.get("/api/shipments/<sid>/events")
    def shipment_events(sid):
        _shipment_or_404(db(), sid)
        events = db().query(ShipmentEvent).filter_by(shipment_id=sid).order_by(ShipmentEvent.id.asc()).all()
        return jsonify(shipment_id=sid, events=[e.to_dict() for e in events])

    @app.get("/api/tracking/reasons")
    def tracking_reasons():
        return jsonify(reasons=tracking.list_reasons(db(), request.args.get("category")))

    @app.post("/api/admin/tracking-reasons")
    def add_tracking_reason():
        data = get_json()
        require(data, "code", "label")
        category = data.get("category", "delay")
        if category not in ("delay", "reroute"):
            raise ApiError("category must be 'delay' or 'reroute'", status_code=422, code="validation_error")
        session = db()
        if session.query(DelayReason).filter_by(code=data["code"]).first():
            raise ApiError("reason code already exists", status_code=409, code="conflict")
        reason = DelayReason(code=data["code"], label=data["label"], category=category, active=True)
        session.add(reason)
        session.commit()
        return jsonify(reason.to_dict()), 201

    # --- public holidays / business-day calendar -------------------------------
    @app.get("/api/holidays")
    def public_holidays():
        from datetime import date as _date
        region_code = request.args.get("region") or app.config["DEFAULT_REGION_CODE"]
        year = int(request.args.get("year", _date.today().year))
        return jsonify(region_code=region_code, year=year,
                       source=app.config["SETTINGS"].holiday_provider,
                       holidays=calendar_service.resolve_year(db(), region_code, year,
                                                              provider=app.config["HOLIDAY_PROVIDER"]))

    @app.get("/api/calendar/business-day")
    def business_day():
        d = _parse_iso_date(request.args.get("date")) or _today_date()
        region_code = request.args.get("region") or app.config["DEFAULT_REGION_CODE"]
        provider = app.config["HOLIDAY_PROVIDER"]
        info = calendar_service.schedule_info(db(), region_code, d, provider=provider)
        if request.args.get("add"):
            n = int(request.args["add"])
            info["plus_business_days"] = {
                "n": n,
                "date": calendar_service.add_business_days(db(), region_code, d, n, provider=provider).isoformat(),
            }
        return jsonify(info)

    # --- compliance config (admin) ---------------------------------------------
    @app.post("/api/admin/agencies")
    def add_agency():
        data = get_json()
        require(data, "code", "name", "agency_type")
        if data["agency_type"] not in ("licensing", "insurance", "inspection", "permit"):
            raise ApiError("invalid agency_type", status_code=422, code="validation_error")
        session = db()
        if session.query(ApprovedAgency).filter_by(code=data["code"]).first():
            raise ApiError("agency code already exists", status_code=409, code="conflict")
        agency = ApprovedAgency(code=data["code"], name=data["name"], agency_type=data["agency_type"],
                                region_code=data.get("region_code", "*"), active=bool(data.get("active", True)))
        session.add(agency)
        session.commit()
        return jsonify(agency.to_dict()), 201

    @app.get("/api/admin/agencies")
    def list_agencies():
        q = db().query(ApprovedAgency).order_by(ApprovedAgency.code)
        if request.args.get("region_code"):
            q = q.filter_by(region_code=request.args["region_code"])
        return jsonify(agencies=[a.to_dict() for a in q.all()])

    @app.post("/api/admin/region-rules")
    def upsert_region_rule():
        data = get_json()
        require(data, "region_code")
        session = db()
        rule = session.query(RegionComplianceRule).filter_by(region_code=data["region_code"]).first()
        if rule is None:
            rule = RegionComplianceRule(region_code=data["region_code"])
            session.add(rule)
        for f in ("required_driver_docs", "required_vehicle_docs", "min_experience_years",
                  "min_insured_value", "inspection_interval_days", "require_tracker",
                  "require_onboard_camera", "active"):
            if f in data:
                setattr(rule, f, data[f])
        session.commit()
        return jsonify(rule.to_dict())

    @app.get("/api/admin/region-rules")
    def list_region_rules():
        return jsonify(region_rules=[r.to_dict() for r in db().query(RegionComplianceRule).all()])

    # --- onboarding entities ---------------------------------------------------
    @app.post("/api/drivers")
    def register_driver():
        data = get_json()
        require(data, "driver_id")
        session = db()
        if session.query(Driver).filter_by(driver_id=data["driver_id"]).first():
            raise ApiError("driver already exists", status_code=409, code="conflict")
        driver = Driver(driver_id=data["driver_id"], name=data.get("name"), owner_id=data.get("owner_id"),
                        region_code=data.get("region_code"),
                        experience_years=float(data.get("experience_years", 0) or 0),
                        compliance_status="pending")
        session.add(driver)
        session.commit()
        return jsonify(driver.to_dict()), 201

    @app.post("/api/service-centers")
    def register_center():
        data = get_json()
        require(data, "center_id")
        session = db()
        if session.query(ServiceCenter).filter_by(center_id=data["center_id"]).first():
            raise ApiError("service center already exists", status_code=409, code="conflict")
        center = ServiceCenter(center_id=data["center_id"], name=data.get("name"),
                               owner_id=data.get("owner_id"), region_code=data.get("region_code"),
                               compliance_status="pending")
        session.add(center)
        session.commit()
        return jsonify(center.to_dict()), 201

    @app.post("/api/compliance/documents")
    def add_document():
        data = get_json()
        require(data, "entity_type", "entity_id", "doc_type")
        if data["entity_type"] not in ("driver", "vehicle", "service_center"):
            raise ApiError("invalid entity_type", status_code=422, code="validation_error")
        doc_hash = data.get("doc_hash")
        if not doc_hash and data.get("reference"):
            doc_hash = hashlib.sha256(str(data["reference"]).encode()).hexdigest()[:32]
        doc = ComplianceDocument(
            entity_type=data["entity_type"], entity_id=data["entity_id"], doc_type=data["doc_type"],
            reference=data.get("reference"), issuer_code=data.get("issuer_code"),
            issued_on=data.get("issued_on"), expiry_on=data.get("expiry_on"),
            insured_value=data.get("insured_value"), coverage=data.get("coverage", []),
            doc_hash=doc_hash, verified=bool(data.get("verified", False)))
        session = db()
        session.add(doc)
        session.commit()
        return jsonify(doc.to_dict()), 201

    @app.post("/api/vehicles/<vid>/inspection")
    def add_inspection(vid):
        data = get_json()
        require(data, "center_id", "result")
        if data["result"] not in ("pass", "fail"):
            raise ApiError("result must be pass or fail", status_code=422, code="validation_error")
        session = db()
        if session.query(Vehicle).filter_by(vehicle_id=vid).first() is None:
            raise ApiError("vehicle not found", status_code=404, code="not_found")
        insp = Inspection(vehicle_id=vid, center_id=data["center_id"],
                          performed_on=data.get("performed_on", _today_date().isoformat()),
                          result=data["result"], valid_until=data.get("valid_until"),
                          report_ref=data.get("report_ref"))
        session.add(insp)
        session.commit()
        return jsonify(insp.to_dict()), 201

    # --- compliance evaluation -------------------------------------------------
    @app.post("/api/drivers/<did>/submit")
    def submit_driver(did):
        session = db()
        driver = session.query(Driver).filter_by(driver_id=did).first()
        if driver is None:
            raise ApiError("driver not found", status_code=404, code="not_found")
        return jsonify(compliance_service.evaluate_driver(session, driver).to_dict())

    @app.post("/api/vehicles/<vid>/submit")
    def submit_vehicle(vid):
        session = db()
        vehicle = session.query(Vehicle).filter_by(vehicle_id=vid).first()
        if vehicle is None:
            raise ApiError("vehicle not found", status_code=404, code="not_found")
        return jsonify(compliance_service.evaluate_vehicle(session, vehicle).to_dict())

    @app.post("/api/service-centers/<cid>/submit")
    def submit_center(cid):
        session = db()
        center = session.query(ServiceCenter).filter_by(center_id=cid).first()
        if center is None:
            raise ApiError("service center not found", status_code=404, code="not_found")
        return jsonify(compliance_service.evaluate_service_center(session, center).to_dict())

    @app.get("/api/compliance/decisions")
    def list_decisions():
        q = db().query(ComplianceDecision).order_by(ComplianceDecision.id.desc())
        for f in ("entity_type", "entity_id", "decision"):
            if request.args.get(f):
                q = q.filter_by(**{f: request.args[f]})
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(decisions=[d.to_dict() for d in items], pagination=meta)

    @app.post("/api/admin/compliance/decisions/<int:decision_id>/override")
    def override_decision(decision_id):
        data = get_json()
        require(data, "decision", "decided_by")
        if data["decision"] not in ("approved", "rejected", "review"):
            raise ApiError("invalid decision", status_code=422, code="validation_error")
        try:
            row = compliance_service.override(db(), decision_id, data["decision"],
                                              data["decided_by"], data.get("note"))
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="not_found")
        return jsonify(row.to_dict())

    # --- load planning: matching & consolidation -------------------------------
    @app.get("/api/loads/<ref>/match-vehicles")
    def match_vehicles(ref):
        session = db()
        load = _load_or_404(session, ref)
        require_approved = app.config["COMPLIANCE_ENFORCED"] or request.args.get("approved") == "true"
        return jsonify(load_ref=ref, load_mode=load.load_mode, scope=load.scope,
                       matches=loads_planning.matchable_vehicles(session, load, require_approved=require_approved))

    @app.get("/api/consolidations/suggest")
    def suggest_consolidations():
        max_w = request.args.get("max_group_weight_kg")
        groups = loads_planning.suggest_consolidations(
            db(), region_code=request.args.get("region"),
            max_group_weight_kg=float(max_w) if max_w else None)
        return jsonify(groups=groups)

    @app.post("/api/consolidations")
    def create_consolidation():
        data = get_json()
        require(data, "load_refs")
        try:
            con = loads_planning.create_consolidation(db(), data["load_refs"])
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(con.to_dict()), 201

    @app.get("/api/consolidations")
    def list_consolidations():
        q = db().query(Consolidation).order_by(Consolidation.id.desc())
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(consolidations=[c.to_dict() for c in items], pagination=meta)

    return app


def _today_date():
    from datetime import date as _date
    return _date.today()


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
    # threaded so SSE streams don't block other requests in the dev server.
    app.run(host="0.0.0.0", port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
