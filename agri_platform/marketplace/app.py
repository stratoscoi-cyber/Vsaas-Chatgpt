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

from ..common.channels import build_channels
from ..common.eventbus import make_event_bus
from ..common.holiday_provider import make_holiday_provider
from . import (
    calendar_service, compliance_service, demand, fleet, insurance, kyc, loads_planning,
    negotiation, onboarding, payments, reputation, resilience, service, service_centers,
    tax_service, telematics, tenants, tracking, trust,
)
from .routing_client import make_traas_client
from .models import (
    ApprovedAgency, Base, ComplianceDecision, ComplianceDocument, Consolidation,
    DelayReason, Driver, Escrow, Holiday, Inspection, InsuranceClaim, InsurancePolicy,
    InsuranceRate, KycCheck, Load, NegotiationMessage, Offer, Party, RegionComplianceRule,
    RoleRequirement, ServiceCenter, Shipment, ShipmentEvent, TaxRule, TrackPoint, Vehicle,
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
    bus = make_event_bus(settings.redis_url)
    app.config["EVENT_BUS"] = bus
    base_notifier = tracking_notifier or tracking.make_tracking_notifier(settings)
    app.config["TRACKING_NOTIFIER"] = tracking.CompositeNotifier([tracking.BusTrackingNotifier(bus), base_notifier])
    app.config["COMPLIANCE_ENFORCED"] = settings.compliance_enforced
    app.config["TENANT_ISOLATION"] = settings.tenant_isolation
    tenants.install_tenant_guard(session_factory, Base, lambda: app.config["TENANT_ISOLATION"])
    app.config["KYC_PROVIDER"] = kyc.make_kyc_provider(settings)
    app.config["PAYMENT_GATEWAY"] = payments.ManualGateway()
    app.config["CHANNELS"] = build_channels({
        "sms": getattr(settings, "sms_webhook_url", None),
        "whatsapp": getattr(settings, "whatsapp_webhook_url", None),
        "push": getattr(settings, "push_webhook_url", None),
    })

    install_request_logging(app, SERVICE_NAME)
    install_auth(app, settings.api_keys, settings.auth_enabled)
    install_rate_limiting(app, RateLimiter(settings.rate_limit_per_minute))
    install_admin_auth(app, settings.admin_api_keys)
    install_localization(app, settings.default_language)
    register_localization(app)
    register_error_handlers(app)
    resilience.install_resilience(app)
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
        load = tenants.scope(session.query(Load), Load).filter(Load.ref == ref).first()
        if load is None:
            raise ApiError("load not found", status_code=404, code="not_found")
        return load

    def _offer_or_404(session, offer_id) -> Offer:
        offer = tenants.scope(session.query(Offer), Offer).filter(Offer.id == offer_id).first()
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
        tenants.stamp(load)
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
        q = tenants.scope(db().query(Load), Load).order_by(Load.id.desc())
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
        region_code = load.origin_region or app.config["DEFAULT_REGION_CODE"]
        demand_info = demand.surge_factor(session, region_code)
        carbon = demand.carbon_estimate(est.distance_km, load.weight_kg, data.get("vehicle_type"))
        return jsonify(load_ref=ref, estimate=est_dict, tax=tax, demand=demand_info, carbon=carbon)

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
        tenants.stamp(vehicle)
        session.add(vehicle)
        session.commit()
        return jsonify(vehicle.to_dict()), 201

    @app.get("/api/vehicles")
    def list_vehicles():
        q = tenants.scope(db().query(Vehicle), Vehicle).order_by(Vehicle.id.desc())
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
        if tenants.stamp(offer).tenant_id:
            session.commit()
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
        escrow = None
        if load.origin and load.destination:
            shipment = tracking.create_shipment(
                session, load, offer,
                driver_name=data.get("driver_name"),
                driver_phone=data.get("driver_phone"),
                avg_speed_kmh=data.get("avg_speed_kmh"),
                notifier=app.config["TRACKING_NOTIFIER"],
            )
            escrow = payments.open_escrow(
                session, shipment_id=shipment.shipment_id, load_ref=load.ref,
                payer_id=load.shipper_id, payee_id=offer.bidder_id, amount=offer.price,
                currency=offer.currency or load.currency or "USD",
                gateway=app.config["PAYMENT_GATEWAY"])
            if tenants.current_tenant():
                tenants.stamp(shipment)
                tenants.stamp(escrow)
                session.commit()
        return jsonify(offer=offer.to_dict(), load=load.to_dict(),
                       shipment=shipment.to_dict() if shipment else None,
                       escrow=escrow.to_dict() if escrow else None)

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
        shipment = tenants.scope(session.query(Shipment), Shipment).filter(Shipment.shipment_id == sid).first()
        if shipment is None:
            raise ApiError("shipment not found", status_code=404, code="not_found")
        return shipment

    @app.get("/api/shipments")
    def list_shipments():
        q = tenants.scope(db().query(Shipment), Shipment).order_by(Shipment.id.desc())
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
        for f in ("required_driver_docs", "required_vehicle_docs", "required_service_center_docs",
                  "min_experience_years", "min_insured_value", "inspection_interval_days",
                  "require_tracker", "require_onboard_camera", "active"):
            if f in data:
                setattr(rule, f, data[f])
        session.commit()
        return jsonify(rule.to_dict())

    @app.get("/api/admin/region-rules")
    def list_region_rules():
        return jsonify(region_rules=[r.to_dict() for r in db().query(RegionComplianceRule).all()])

    # --- role requirements (admin; drivers use region-rules above) -------------
    @app.post("/api/admin/role-requirements")
    def upsert_role_requirement():
        data = get_json()
        require(data, "region_code", "role")
        if data["role"] not in onboarding.PARTY_ROLES:
            raise ApiError("role must be one of " + ", ".join(onboarding.PARTY_ROLES),
                           status_code=422, code="validation_error")
        session = db()
        rr = session.query(RoleRequirement).filter_by(region_code=data["region_code"], role=data["role"]).first()
        if rr is None:
            rr = RoleRequirement(region_code=data["region_code"], role=data["role"])
            session.add(rr)
        for f in ("required_docs", "required_screening", "min_experience_years",
                  "needs_accreditation", "active"):
            if f in data:
                setattr(rr, f, data[f])
        session.commit()
        return jsonify(rr.to_dict())

    @app.get("/api/admin/role-requirements")
    def list_role_requirements():
        q = db().query(RoleRequirement)
        if request.args.get("role"):
            q = q.filter_by(role=request.args["role"])
        return jsonify(role_requirements=[r.to_dict() for r in q.all()])

    # --- generic party onboarding (operators, fleet managers, agents, MSPs) ----
    def _party_or_404(session, pid) -> Party:
        party = session.query(Party).filter_by(party_id=pid).first()
        if party is None:
            raise ApiError("party not found", status_code=404, code="not_found")
        return party

    @app.post("/api/parties")
    def register_party():
        data = get_json()
        require(data, "party_id", "party_type")
        if data["party_type"] not in onboarding.PARTY_ROLES:
            raise ApiError("party_type must be one of " + ", ".join(onboarding.PARTY_ROLES),
                           status_code=422, code="validation_error")
        session = db()
        if session.query(Party).filter_by(party_id=data["party_id"]).first():
            raise ApiError("party already exists", status_code=409, code="conflict")
        party = Party(party_id=data["party_id"], party_type=data["party_type"], name=data.get("name"),
                      owner_id=data.get("owner_id"), parent_id=data.get("parent_id"),
                      region_code=data.get("region_code"),
                      experience_years=float(data.get("experience_years", 0) or 0),
                      compliance_status="pending")
        tenants.stamp(party)
        session.add(party)
        session.commit()
        return jsonify(party.to_dict()), 201

    @app.get("/api/parties")
    def list_parties():
        q = db().query(Party).order_by(Party.id.desc())
        for f in ("party_type", "compliance_status", "region_code", "owner_id"):
            if request.args.get(f):
                q = q.filter_by(**{f: request.args[f]})
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(parties=[p.to_dict() for p in items], pagination=meta)

    @app.get("/api/parties/<pid>")
    def get_party(pid):
        session = db()
        party = _party_or_404(session, pid)
        return jsonify(onboarding.profile(session, party, role=party.party_type, entity_id=pid))

    @app.post("/api/parties/<pid>/submit")
    def submit_party(pid):
        session = db()
        party = _party_or_404(session, pid)
        return jsonify(onboarding.submit(
            session, party, role=party.party_type, entity_id=pid, region_code=party.region_code,
            name=party.name, experience_years=party.experience_years, owner_id=party.owner_id,
            kyc_provider=app.config["KYC_PROVIDER"]))

    @app.get("/api/parties/<pid>/risk")
    def party_risk(pid):
        session = db()
        party = _party_or_404(session, pid)
        return jsonify(onboarding.risk_assessment(
            session, role=party.party_type, entity_id=pid, owner_id=party.owner_id,
            reasons=[], screening_clear=None, screening_required=False))

    @app.post("/api/admin/parties/<pid>/status")
    def transition_party(pid):
        data = get_json()
        require(data, "action", "decided_by")
        session = db()
        party = _party_or_404(session, pid)
        try:
            out = onboarding.transition(session, party, role=party.party_type, entity_id=pid,
                                        action=data["action"], reason=data.get("reason"),
                                        decided_by=data["decided_by"])
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(out)

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
        tenants.stamp(driver)
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
                               business_reg_no=data.get("business_reg_no"), compliance_status="pending")
        tenants.stamp(center)
        session.add(center)
        session.commit()
        return jsonify(center.to_dict()), 201

    @app.get("/api/service-centers")
    def list_service_centers():
        q = db().query(ServiceCenter).order_by(ServiceCenter.id.desc())
        for f in ("region_code", "compliance_status", "owner_id"):
            if request.args.get(f):
                q = q.filter_by(**{f: request.args[f]})
        page, size = page_params()
        items, meta = paginate(q, page, size)
        return jsonify(service_centers=[c.to_dict() for c in items], pagination=meta)

    def _center_or_404(session, cid) -> ServiceCenter:
        center = session.query(ServiceCenter).filter_by(center_id=cid).first()
        if center is None:
            raise ApiError("service center not found", status_code=404, code="not_found")
        return center

    @app.get("/api/service-centers/<cid>")
    def get_service_center(cid):
        return jsonify(service_centers.profile(db(), _center_or_404(db(), cid)))

    @app.get("/api/service-centers/<cid>/risk")
    def service_center_risk(cid):
        session = db()
        center = _center_or_404(session, cid)
        return jsonify(service_centers.risk_assessment(session, center))

    @app.get("/api/service-centers/<cid>/reputation")
    def service_center_reputation(cid):
        _center_or_404(db(), cid)
        return jsonify(reputation.reputation(db(), "service_center", cid))

    @app.post("/api/admin/service-centers/<cid>/status")
    def transition_service_center(cid):
        data = get_json()
        require(data, "action", "decided_by")
        session = db()
        center = _center_or_404(session, cid)
        try:
            out = service_centers.transition(session, center, data["action"],
                                             reason=data.get("reason"), decided_by=data["decided_by"])
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(out)

    @app.post("/api/compliance/documents")
    def add_document():
        data = get_json()
        require(data, "entity_type", "entity_id", "doc_type")
        if data["entity_type"] not in onboarding.ONBOARDING_ENTITY_TYPES:
            raise ApiError("invalid entity_type", status_code=422, code="validation_error",
                           details={"allowed": list(onboarding.ONBOARDING_ENTITY_TYPES)})
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
        session.flush()
        kyc_result = None
        # Auto-verify via a configured KYC provider (manual provider verifies nothing).
        if data.get("auto_verify", True):
            kyc_result = _run_kyc(session, doc)
        session.commit()
        body = doc.to_dict()
        if kyc_result is not None:
            body["kyc"] = kyc_result.to_dict()
        return jsonify(body), 201

    def _run_kyc(session, doc):
        provider = app.config["KYC_PROVIDER"]
        result = provider.verify_document(doc.to_dict())
        if result.status not in ("manual_review_required",):
            # Only a provider verdict changes the flag; manual leaves it as submitted.
            doc.verified = result.verified
        session.add(KycCheck(document_id=doc.id, entity_type=doc.entity_type, entity_id=doc.entity_id,
                             provider=getattr(provider, "name", "manual"), kind="document",
                             verified=result.verified, status=result.status, details=result.details))
        return result

    @app.post("/api/compliance/documents/<int:doc_id>/verify")
    def verify_document(doc_id):
        session = db()
        doc = session.query(ComplianceDocument).filter_by(id=doc_id).first()
        if doc is None:
            raise ApiError("document not found", status_code=404, code="not_found")
        result = _run_kyc(session, doc)
        session.commit()
        return jsonify(document=doc.to_dict(), kyc=result.to_dict())

    @app.post("/api/compliance/screen")
    def screen_identity():
        data = get_json()
        require(data, "name")
        result = app.config["KYC_PROVIDER"].screen(data)
        session = db()
        session.add(KycCheck(entity_type=data.get("entity_type"), entity_id=data.get("entity_id"),
                             provider=getattr(app.config["KYC_PROVIDER"], "name", "manual"),
                             kind="screening", verified=result.clear, status=result.status,
                             details={"hits": result.hits}))
        session.commit()
        return jsonify(result.to_dict())

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
    def _driver_or_404(session, did) -> Driver:
        driver = session.query(Driver).filter_by(driver_id=did).first()
        if driver is None:
            raise ApiError("driver not found", status_code=404, code="not_found")
        return driver

    @app.post("/api/drivers/<did>/submit")
    def submit_driver(did):
        session = db()
        driver = _driver_or_404(session, did)
        return jsonify(onboarding.submit(
            session, driver, role="driver", entity_id=did, region_code=driver.region_code,
            name=driver.name, experience_years=driver.experience_years, owner_id=driver.owner_id,
            kyc_provider=app.config["KYC_PROVIDER"]))

    @app.get("/api/drivers/<did>")
    def get_driver(did):
        session = db()
        return jsonify(onboarding.profile(session, _driver_or_404(session, did), role="driver", entity_id=did))

    @app.post("/api/admin/drivers/<did>/status")
    def transition_driver(did):
        data = get_json()
        require(data, "action", "decided_by")
        session = db()
        driver = _driver_or_404(session, did)
        try:
            out = onboarding.transition(session, driver, role="driver", entity_id=did,
                                        action=data["action"], reason=data.get("reason"),
                                        decided_by=data["decided_by"])
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(out)

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
        center = _center_or_404(session, cid)
        return jsonify(service_centers.submit(session, center))

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

    # --- reputation & ratings --------------------------------------------------
    @app.post("/api/ratings")
    def add_rating():
        data = get_json()
        require(data, "subject_type", "subject_id", "score")
        try:
            r = reputation.add_rating(db(), subject_type=data["subject_type"], subject_id=data["subject_id"],
                                      score=data["score"], rater_id=data.get("rater_id"),
                                      shipment_id=data.get("shipment_id"), comment=data.get("comment"))
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(r.to_dict()), 201

    @app.get("/api/carriers/<carrier_id>/reputation")
    def carrier_reputation(carrier_id):
        return jsonify(reputation.reputation(db(), "carrier", carrier_id))

    @app.get("/api/<subject_type>/<subject_id>/reputation")
    def subject_reputation(subject_type, subject_id):
        if subject_type not in ("carrier", "driver", "shipper"):
            raise ApiError("invalid subject_type", status_code=422, code="validation_error")
        return jsonify(reputation.reputation(db(), subject_type, subject_id))

    # --- telematics ------------------------------------------------------------
    @app.post("/api/telematics/heartbeat")
    def telematics_heartbeat():
        data = get_json()
        require(data, "vehicle_id")
        try:
            v = telematics.heartbeat(
                db(), data["vehicle_id"], lat=data.get("lat"), lon=data.get("lon"),
                speed_kmh=data.get("speed_kmh"), heading=data.get("heading"),
                odometer_km=data.get("odometer_km"), fuel_level=data.get("fuel_level"),
                shipment_id=data.get("shipment_id"), notifier=app.config["TRACKING_NOTIFIER"])
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="not_found")
        return jsonify(v)

    @app.post("/api/telematics/event")
    def telematics_event():
        data = get_json()
        require(data, "vehicle_id", "event_type")
        try:
            res = telematics.record_event(db(), data["vehicle_id"], data["event_type"],
                                          severity=data.get("severity", "info"), data=data.get("data"))
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="not_found")
        return jsonify(res), 201

    @app.get("/api/vehicles/<vid>/telematics")
    def vehicle_telematics(vid):
        try:
            return jsonify(telematics.status(db(), vid))
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="not_found")

    # --- payments: escrow & ePOD ----------------------------------------------
    @app.get("/api/escrows/<escrow_id>")
    def get_escrow(escrow_id):
        e = db().query(Escrow).filter_by(escrow_id=escrow_id).first()
        if e is None:
            raise ApiError("escrow not found", status_code=404, code="not_found")
        return jsonify(e.to_dict())

    @app.get("/api/shipments/<sid>/escrow")
    def shipment_escrow(sid):
        e = db().query(Escrow).filter_by(shipment_id=sid).order_by(Escrow.id.desc()).first()
        if e is None:
            raise ApiError("no escrow for shipment", status_code=404, code="not_found")
        return jsonify(e.to_dict())

    @app.post("/api/shipments/<sid>/epod")
    def post_epod(sid):
        data = get_json()
        session = db()
        if session.query(Shipment).filter_by(shipment_id=sid).first() is None:
            raise ApiError("shipment not found", status_code=404, code="not_found")
        pod = payments.record_epod(session, sid, recipient_name=data.get("recipient_name"),
                                   signature_ref=data.get("signature_ref"),
                                   photo_refs=data.get("photo_refs", []), notes=data.get("notes"),
                                   delivered_at=data.get("delivered_at"))
        return jsonify(pod.to_dict()), 201

    @app.post("/api/escrows/<escrow_id>/release")
    def release_escrow(escrow_id):
        session = db()
        e = session.query(Escrow).filter_by(escrow_id=escrow_id).first()
        if e is None:
            raise ApiError("escrow not found", status_code=404, code="not_found")
        load = session.query(Load).filter_by(ref=e.load_ref).first()
        region_code = (load.origin_region if load else None) or app.config["DEFAULT_REGION_CODE"]
        try:
            settlement = payments.release_escrow(session, e, region_code=region_code,
                                                 gateway=app.config["PAYMENT_GATEWAY"])
        except ValueError as exc:
            raise ApiError(str(exc), status_code=409, code="conflict")
        return jsonify(escrow=e.to_dict(), settlement=settlement.to_dict())

    # --- insurance marketplace -------------------------------------------------
    @app.post("/api/admin/insurance-rates")
    def add_insurance_rate():
        data = get_json()
        require(data, "code", "insurer_code", "level", "rate_percent")
        session = db()
        if session.query(InsuranceRate).filter_by(code=data["code"]).first():
            raise ApiError("rate code already exists", status_code=409, code="conflict")
        rate = InsuranceRate(code=data["code"], insurer_code=data["insurer_code"],
                             region_code=data.get("region_code", "*"), level=data["level"],
                             rate_percent=float(data["rate_percent"]),
                             min_premium=float(data.get("min_premium", 0)), active=True)
        session.add(rate)
        session.commit()
        return jsonify(rate.to_dict()), 201

    @app.post("/api/insurance/quote")
    def insurance_quote():
        data = get_json()
        require(data, "sum_insured")
        region = data.get("region_code")
        if not region and data.get("origin"):
            r = regions.region_for_coords(data["origin"])
            region = r.code if r else app.config["DEFAULT_REGION_CODE"]
        quotes = insurance.quote(db(), sum_insured=float(data["sum_insured"]), region_code=region,
                                 level=data.get("level"), currency=data.get("currency", "USD"),
                                 locale=current_lang())
        return jsonify(quotes=quotes)

    @app.post("/api/insurance/bind")
    def insurance_bind():
        data = get_json()
        require(data, "load_ref", "insurer_code", "level", "sum_insured", "premium", "currency")
        policy = insurance.bind(db(), load_ref=data["load_ref"], insurer_code=data["insurer_code"],
                                level=data["level"], sum_insured=float(data["sum_insured"]),
                                premium=float(data["premium"]), currency=data["currency"],
                                shipment_id=data.get("shipment_id"))
        return jsonify(policy.to_dict()), 201

    @app.post("/api/insurance/claims")
    def insurance_claim():
        data = get_json()
        require(data, "policy_id", "reason", "amount")
        try:
            claim = insurance.open_claim(db(), policy_id=data["policy_id"], reason=data["reason"],
                                         amount=float(data["amount"]), shipment_id=data.get("shipment_id"),
                                         evidence=data.get("evidence", []))
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="not_found")
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(claim.to_dict()), 201

    @app.post("/api/insurance/claims/<claim_id>/status")
    def insurance_claim_status(claim_id):
        data = get_json()
        require(data, "status")
        try:
            claim = insurance.update_claim(db(), claim_id, data["status"])
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="not_found")
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(claim.to_dict())

    # --- demand / dynamic pricing ----------------------------------------------
    @app.get("/api/lanes/<origin>/<destination>/benchmark")
    def lane_benchmark(origin, destination):
        return jsonify(origin_region=origin, destination_region=destination,
                       benchmark=demand.lane_benchmark(db(), origin, destination))

    @app.get("/api/regions/<code>/surge")
    def region_surge(code):
        return jsonify(demand.surge_factor(db(), code))

    @app.get("/api/loads/<ref>/backhaul")
    def load_backhaul(ref):
        session = db()
        load = _load_or_404(session, ref)
        radius = float(request.args.get("radius_km", 75))
        return jsonify(load_ref=ref, candidates=demand.backhaul_candidates(session, load, radius_km=radius))

    # --- fleet & driver capacity ----------------------------------------------
    @app.get("/api/vehicles/<vid>/service-status")
    def vehicle_service_status(vid):
        v = db().query(Vehicle).filter_by(vehicle_id=vid).first()
        if v is None:
            raise ApiError("vehicle not found", status_code=404, code="not_found")
        return jsonify(fleet.service_status(db(), v))

    @app.post("/api/drivers/<did>/duty")
    def driver_duty(did):
        data = get_json()
        require(data, "status")
        try:
            seg = fleet.log_duty(db(), did, data["status"])
        except ValueError as exc:
            raise ApiError(str(exc), status_code=422, code="validation_error")
        return jsonify(seg.to_dict()), 201

    @app.get("/api/drivers/<did>/hours")
    def driver_hours(did):
        window = int(request.args.get("window_hours", 24))
        return jsonify(fleet.driving_hours(db(), did, window_hours=window))

    # --- trust & safety --------------------------------------------------------
    @app.get("/api/ops/review-queue")
    def ops_review_queue():
        return jsonify(queue=trust.review_queue(db()))

    @app.get("/api/trust/clusters")
    def trust_clusters():
        return jsonify(clusters=trust.collusion_clusters(db()))

    # --- notifications channels (SMS/WhatsApp/push) ----------------------------
    @app.post("/api/notifications/send")
    def send_notification():
        data = get_json()
        require(data, "channel", "to", "message")
        channel = app.config["CHANNELS"].get(data["channel"])
        if channel is None:
            raise ApiError("unknown channel", status_code=422, code="validation_error",
                           details={"channels": list(app.config["CHANNELS"].keys())})
        ok = channel.send(data["to"], data["message"], data.get("meta"))
        return jsonify(channel=data["channel"], delivered=ok)

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
