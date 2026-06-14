"""SQLAlchemy models for the logistics marketplace."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()


class Load(Base):
    """A shipment posted by a shipper that needs to be moved."""

    __tablename__ = "loads"

    id = Column(Integer, primary_key=True)
    ref = Column(String(50), unique=True, nullable=False, index=True)
    tenant_id = Column(String(50), index=True)
    shipper_id = Column(String(50), index=True)
    title = Column(String(160))
    origin = Column(JSON)        # {"lat","lon","address"}
    destination = Column(JSON)   # {"lat","lon","address"}
    weight_kg = Column(Float)
    dimensions_cm = Column(JSON)  # {"length","width","height"}
    quantity = Column(Integer, default=1)
    load_type = Column(String(40))       # perishable_produce|grain|livestock|equipment|fertilizer|fuel|general
    classifications = Column(JSON, default=list)  # [refrigerated, hazardous, fragile, oversized, livestock]
    pickup_window = Column(JSON)         # {"from","to"}
    delivery_deadline = Column(String(40))
    budget = Column(Float)               # optional target price
    currency = Column(String(8), default="USD")
    distance_km = Column(Float)          # optional precomputed; else derived
    # Load mode and geographic scope (see loads_planning).
    load_mode = Column(String(16), default="ftl")   # ftl | ltl | consolidation
    scope = Column(String(16))                       # local | intra_region | inter_region
    origin_region = Column(String(8))
    destination_region = Column(String(8))
    consolidation_id = Column(String(50), index=True)
    # Optional cargo insurance selected by the load owner at placement.
    insurance_opted = Column(Boolean, default=False)
    insurance_level = Column(String(16))             # basic | premium
    insurance_value = Column(Float)
    status = Column(String(20), default="open")  # open|negotiating|awarded|in_transit|delivered|cancelled
    awarded_offer_id = Column(Integer)
    notes = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "ref": self.ref,
            "tenant_id": self.tenant_id,
            "shipper_id": self.shipper_id,
            "title": self.title,
            "origin": self.origin,
            "destination": self.destination,
            "weight_kg": self.weight_kg,
            "dimensions_cm": self.dimensions_cm,
            "quantity": self.quantity,
            "load_type": self.load_type,
            "classifications": self.classifications or [],
            "pickup_window": self.pickup_window,
            "delivery_deadline": self.delivery_deadline,
            "budget": self.budget,
            "currency": self.currency,
            "distance_km": self.distance_km,
            "load_mode": self.load_mode,
            "scope": self.scope,
            "origin_region": self.origin_region,
            "destination_region": self.destination_region,
            "consolidation_id": self.consolidation_id,
            "insurance_opted": self.insurance_opted,
            "insurance_level": self.insurance_level,
            "insurance_value": self.insurance_value,
            "status": self.status,
            "awarded_offer_id": self.awarded_offer_id,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Vehicle(Base):
    """A vehicle registered by a transporter / vehicle owner."""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(String(50), unique=True, nullable=False, index=True)
    tenant_id = Column(String(50), index=True)
    owner_id = Column(String(50), index=True)
    vehicle_type = Column(String(40))  # pickup|truck_small|truck_large|refrigerated_truck|flatbed|tanker|livestock_carrier
    capacity_kg = Column(Float)
    capacity_m3 = Column(Float)
    features = Column(JSON, default=list)  # [refrigeration, crane, livestock_rated, hazmat_certified, gps]
    base_rate_per_km = Column(Float)
    location = Column(JSON)
    available = Column(String(10), default="true")
    region_code = Column(String(8), index=True)
    driver_id = Column(String(50), index=True)
    # Onboarding / compliance state.
    compliance_status = Column(String(16))  # None|pending|approved|rejected|review|suspended
    tracker_serial = Column(String(60))
    tracker_model = Column(String(60))
    tracker_approved = Column(Boolean, default=False)
    tracker_serviceable = Column(Boolean, default=False)
    camera_serial = Column(String(60))
    inspection_valid_until = Column(String(10))  # ISO date
    # Telematics (from the mandated IoT tracker).
    telematics_status = Column(String(12))   # online|offline|suspected_tamper
    last_heartbeat_at = Column(DateTime)
    odometer_km = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "vehicle_id": self.vehicle_id,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "vehicle_type": self.vehicle_type,
            "capacity_kg": self.capacity_kg,
            "capacity_m3": self.capacity_m3,
            "features": self.features or [],
            "base_rate_per_km": self.base_rate_per_km,
            "location": self.location,
            "available": self.available,
            "region_code": self.region_code,
            "driver_id": self.driver_id,
            "compliance_status": self.compliance_status,
            "tracker_serial": self.tracker_serial,
            "tracker_model": self.tracker_model,
            "tracker_approved": self.tracker_approved,
            "tracker_serviceable": self.tracker_serviceable,
            "camera_serial": self.camera_serial,
            "inspection_valid_until": self.inspection_valid_until,
            "telematics_status": self.telematics_status,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None,
            "odometer_km": self.odometer_km,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Offer(Base):
    """A rate offer (bid) placed against a load."""

    __tablename__ = "offers"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(50), index=True)
    load_ref = Column(String(50), index=True)
    bidder_id = Column(String(50), index=True)
    bidder_role = Column(String(30))  # transporter|vehicle_owner|logistics_manager
    vehicle_id = Column(String(50))
    price = Column(Float)
    currency = Column(String(8), default="USD")
    eta_hours = Column(Float)
    message = Column(String(1000))
    negotiation_style = Column(String(40))
    status = Column(String(20), default="proposed")  # proposed|countered|accepted|rejected|withdrawn
    round = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "load_ref": self.load_ref,
            "bidder_id": self.bidder_id,
            "bidder_role": self.bidder_role,
            "vehicle_id": self.vehicle_id,
            "price": self.price,
            "currency": self.currency,
            "eta_hours": self.eta_hours,
            "message": self.message,
            "negotiation_style": self.negotiation_style,
            "status": self.status,
            "round": self.round,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TaxRule(Base):
    """An operator-configured statutory tax / VAT / levy rule.

    Rates and applicability are entered and maintained by an administrator; the
    system ships with none. ``statutory_reference`` lets the operator record the
    legal basis for audit.
    """

    __tablename__ = "tax_rules"

    id = Column(Integer, primary_key=True)
    code = Column(String(40), unique=True, nullable=False, index=True)  # e.g. "NG-VAT"
    region_code = Column(String(8), index=True)  # ISO-3166 alpha-2, or "*" for all regions
    name = Column(String(120))
    tax_type = Column(String(30))   # vat | gst | withholding | levy | excise | other
    collection = Column(String(12))  # add | withhold
    rate_percent = Column(Float)
    basis = Column(String(12), default="net")  # net | compound
    applies_to = Column(JSON, default=list)     # categories; empty = all
    threshold_min = Column(Float, default=0.0)  # minimum base for the rule to apply
    sequence = Column(Integer, default=0)       # application order
    effective_from = Column(String(10))         # ISO date (inclusive)
    effective_to = Column(String(10))           # ISO date (inclusive); null = open
    statutory_reference = Column(String(300))   # operator-recorded legal basis
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "code": self.code,
            "region_code": self.region_code,
            "name": self.name,
            "tax_type": self.tax_type,
            "collection": self.collection,
            "rate_percent": self.rate_percent,
            "basis": self.basis,
            "applies_to": self.applies_to or [],
            "threshold_min": self.threshold_min,
            "sequence": self.sequence,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "statutory_reference": self.statutory_reference,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_rule(self):
        """Plain dict consumed by the tax engine."""
        return {
            "code": self.code,
            "name": self.name,
            "tax_type": self.tax_type,
            "collection": self.collection,
            "rate_percent": self.rate_percent,
            "basis": self.basis or "net",
            "applies_to": self.applies_to or [],
            "threshold_min": self.threshold_min or 0.0,
            "sequence": self.sequence or 0,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "active": self.active,
        }


class Shipment(Base):
    """A live, trackable shipment created when an offer is awarded."""

    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(String(50), unique=True, nullable=False, index=True)
    tenant_id = Column(String(50), index=True)
    load_ref = Column(String(50), index=True)
    offer_id = Column(Integer)
    carrier_id = Column(String(50))
    vehicle_id = Column(String(50))
    driver_name = Column(String(120))
    driver_phone = Column(String(40))
    region_code = Column(String(8))
    origin = Column(JSON)
    destination = Column(JSON)
    current_location = Column(JSON)  # {"lat","lon"}
    status = Column(String(20), default="assigned")  # assigned|picked_up|en_route|delayed|arrived|delivered|cancelled
    planned_distance_km = Column(Float)
    avg_speed_kmh = Column(Float, default=50.0)
    # Detour multiplier vs. straight-line distance; set after a TRAAS reroute so
    # the avoidance penalty carries forward into the ETA as the driver moves.
    route_factor = Column(Float)
    delay_minutes = Column(Float, default=0.0)
    eta = Column(String(40))  # ISO datetime
    currency = Column(String(8), default="USD")
    price = Column(Float)
    last_update_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "shipment_id": self.shipment_id,
            "load_ref": self.load_ref,
            "offer_id": self.offer_id,
            "carrier_id": self.carrier_id,
            "vehicle_id": self.vehicle_id,
            "driver_name": self.driver_name,
            "driver_phone": self.driver_phone,
            "region_code": self.region_code,
            "origin": self.origin,
            "destination": self.destination,
            "current_location": self.current_location,
            "status": self.status,
            "planned_distance_km": self.planned_distance_km,
            "avg_speed_kmh": self.avg_speed_kmh,
            "route_factor": self.route_factor,
            "delay_minutes": self.delay_minutes,
            "eta": self.eta,
            "currency": self.currency,
            "price": self.price,
            "last_update_at": self.last_update_at.isoformat() if self.last_update_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TrackPoint(Base):
    """A GPS breadcrumb reported by the driver/operator device."""

    __tablename__ = "track_points"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(String(50), index=True)
    lat = Column(Float)
    lon = Column(Float)
    speed_kmh = Column(Float)
    heading = Column(Float)
    source = Column(String(20), default="driver")  # driver|operator|gps
    recorded_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "shipment_id": self.shipment_id,
            "lat": self.lat,
            "lon": self.lon,
            "speed_kmh": self.speed_kmh,
            "heading": self.heading,
            "source": self.source,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
        }


class ShipmentEvent(Base):
    """An event in a shipment's timeline (status/location/delay/reroute/eta)."""

    __tablename__ = "shipment_events"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(String(50), index=True)
    event_type = Column(String(20))  # status|location|delay|reroute|eta|note
    status = Column(String(20))
    reason_code = Column(String(40))
    reason_label = Column(String(160))
    message = Column(String(500))
    delay_minutes = Column(Float)
    eta = Column(String(40))
    location = Column(JSON)
    data = Column(JSON)
    created_by = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "shipment_id": self.shipment_id,
            "event_type": self.event_type,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason_label": self.reason_label,
            "message": self.message,
            "delay_minutes": self.delay_minutes,
            "eta": self.eta,
            "location": self.location,
            "data": self.data,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DelayReason(Base):
    """A selectable delay/reroute reason. Defaults are seeded; admin-extensible."""

    __tablename__ = "delay_reasons"

    id = Column(Integer, primary_key=True)
    code = Column(String(40), unique=True, nullable=False, index=True)
    label = Column(String(160))
    category = Column(String(20), default="delay")  # delay|reroute
    active = Column(Boolean, default=True)

    def to_dict(self):
        return {"code": self.code, "label": self.label, "category": self.category, "active": self.active}


class Holiday(Base):
    """An operator-configured public holiday for a region.

    The platform ships with none; administrators enter the real gazetted dates.
    ``recurrence`` is ``fixed`` (month/day, recurs yearly) or ``date`` (a single
    observed ISO date, for movable/gazetted holidays).
    """

    __tablename__ = "holidays"

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    region_code = Column(String(8), index=True)  # ISO-3166 alpha-2
    name = Column(String(160))
    recurrence = Column(String(10), default="fixed")  # fixed | date
    month = Column(Integer)   # for fixed
    day = Column(Integer)     # for fixed
    date = Column(String(10)) # for one-off (ISO YYYY-MM-DD)
    note = Column(String(300))
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "code": self.code,
            "region_code": self.region_code,
            "name": self.name,
            "recurrence": self.recurrence,
            "month": self.month,
            "day": self.day,
            "date": self.date,
            "note": self.note,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_rule(self):
        return {
            "code": self.code,
            "name": self.name,
            "recurrence": self.recurrence or "fixed",
            "month": self.month,
            "day": self.day,
            "date": self.date,
            "active": self.active,
        }


class NegotiationMessage(Base):
    """A single turn in a load's negotiation thread."""

    __tablename__ = "negotiation_messages"

    id = Column(Integer, primary_key=True)
    load_ref = Column(String(50), index=True)
    offer_id = Column(Integer, index=True)
    author_id = Column(String(50))
    author_role = Column(String(30))  # shipper|bidder|assistant
    price = Column(Float)
    body = Column(String(2000))
    negotiation_style = Column(String(40))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "load_ref": self.load_ref,
            "offer_id": self.offer_id,
            "author_id": self.author_id,
            "author_role": self.author_role,
            "price": self.price,
            "body": self.body,
            "negotiation_style": self.negotiation_style,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --- onboarding / compliance ---------------------------------------------------

class ApprovedAgency(Base):
    """An issuer the operator recognises (licensing, insurance, inspection, permit)."""

    __tablename__ = "approved_agencies"

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(160))
    agency_type = Column(String(20))  # licensing|insurance|inspection|permit
    region_code = Column(String(8), index=True)  # ISO-2 or "*" for all
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"code": self.code, "name": self.name, "agency_type": self.agency_type,
                "region_code": self.region_code, "active": self.active}


class RegionComplianceRule(Base):
    """Per-region onboarding requirements (operator-managed; ships with none)."""

    __tablename__ = "region_compliance_rules"

    id = Column(Integer, primary_key=True)
    region_code = Column(String(8), unique=True, nullable=False, index=True)
    required_driver_docs = Column(JSON, default=list)   # e.g. ["drivers_license","permit","experience_proof"]
    required_vehicle_docs = Column(JSON, default=list)  # e.g. ["registration","insurance","roadworthiness"]
    min_experience_years = Column(Float, default=2.0)
    min_insured_value = Column(Float, default=0.0)
    inspection_interval_days = Column(Integer, default=180)
    require_tracker = Column(Boolean, default=True)
    require_onboard_camera = Column(Boolean, default=True)
    active = Column(Boolean, default=True)

    def to_dict(self):
        return {
            "region_code": self.region_code,
            "required_driver_docs": self.required_driver_docs or [],
            "required_vehicle_docs": self.required_vehicle_docs or [],
            "min_experience_years": self.min_experience_years,
            "min_insured_value": self.min_insured_value,
            "inspection_interval_days": self.inspection_interval_days,
            "require_tracker": self.require_tracker,
            "require_onboard_camera": self.require_onboard_camera,
            "active": self.active,
        }

    def to_rule(self):
        return self.to_dict()


class Driver(Base):
    __tablename__ = "drivers"

    id = Column(Integer, primary_key=True)
    driver_id = Column(String(50), unique=True, nullable=False, index=True)
    tenant_id = Column(String(50), index=True)
    name = Column(String(120))
    owner_id = Column(String(50), index=True)
    region_code = Column(String(8))
    experience_years = Column(Float, default=0.0)
    compliance_status = Column(String(16), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"driver_id": self.driver_id, "name": self.name, "owner_id": self.owner_id,
                "region_code": self.region_code, "experience_years": self.experience_years,
                "compliance_status": self.compliance_status,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class ServiceCenter(Base):
    __tablename__ = "service_centers"

    id = Column(Integer, primary_key=True)
    center_id = Column(String(50), unique=True, nullable=False, index=True)
    tenant_id = Column(String(50), index=True)
    name = Column(String(160))
    owner_id = Column(String(50), index=True)
    region_code = Column(String(8))
    compliance_status = Column(String(16), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"center_id": self.center_id, "name": self.name, "owner_id": self.owner_id,
                "region_code": self.region_code, "compliance_status": self.compliance_status,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class ComplianceDocument(Base):
    """A submitted document for a driver/vehicle/service-center."""

    __tablename__ = "compliance_documents"

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(20), index=True)  # driver|vehicle|service_center
    entity_id = Column(String(50), index=True)
    doc_type = Column(String(40))                 # drivers_license|permit|insurance|registration|...
    reference = Column(String(120))               # document number
    issuer_code = Column(String(50))              # FK -> approved_agencies.code
    issued_on = Column(String(10))
    expiry_on = Column(String(10))
    insured_value = Column(Float)                 # for insurance docs
    coverage = Column(JSON, default=list)         # e.g. ["vehicular","load","third_party"]
    doc_hash = Column(String(80), index=True)     # to detect re-used documents
    verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "entity_type": self.entity_type, "entity_id": self.entity_id,
            "doc_type": self.doc_type, "reference": self.reference, "issuer_code": self.issuer_code,
            "issued_on": self.issued_on, "expiry_on": self.expiry_on, "insured_value": self.insured_value,
            "coverage": self.coverage or [], "doc_hash": self.doc_hash, "verified": self.verified,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_doc(self):
        return self.to_dict()


class Inspection(Base):
    __tablename__ = "inspections"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(String(50), index=True)
    center_id = Column(String(50))
    performed_on = Column(String(10))
    result = Column(String(10))      # pass|fail
    valid_until = Column(String(10))
    report_ref = Column(String(120))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "vehicle_id": self.vehicle_id, "center_id": self.center_id,
                "performed_on": self.performed_on, "result": self.result,
                "valid_until": self.valid_until, "report_ref": self.report_ref,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class ComplianceDecision(Base):
    """An auto/human onboarding decision with its reasons and risk signals."""

    __tablename__ = "compliance_decisions"

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(20), index=True)
    entity_id = Column(String(50), index=True)
    decision = Column(String(16))    # approved|rejected|review
    reasons = Column(JSON, default=list)
    warnings = Column(JSON, default=list)
    signals = Column(JSON, default=list)
    risk_score = Column(Float, default=0.0)
    auto = Column(Boolean, default=True)
    decided_by = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "entity_type": self.entity_type, "entity_id": self.entity_id,
            "decision": self.decision, "reasons": self.reasons or [], "warnings": self.warnings or [],
            "signals": self.signals or [], "risk_score": self.risk_score, "auto": self.auto,
            "decided_by": self.decided_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Consolidation(Base):
    """An aggregation of LTL loads sharing an origin and destination area."""

    __tablename__ = "consolidations"

    id = Column(Integer, primary_key=True)
    consolidation_id = Column(String(50), unique=True, nullable=False, index=True)
    tenant_id = Column(String(50), index=True)
    origin_region = Column(String(8))
    destination_region = Column(String(8))
    load_refs = Column(JSON, default=list)
    total_weight_kg = Column(Float)
    status = Column(String(20), default="open")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"consolidation_id": self.consolidation_id, "origin_region": self.origin_region,
                "destination_region": self.destination_region, "load_refs": self.load_refs or [],
                "total_weight_kg": self.total_weight_kg, "status": self.status,
                "created_at": self.created_at.isoformat() if self.created_at else None}


# --- reputation ----------------------------------------------------------------

class Rating(Base):
    __tablename__ = "ratings"

    id = Column(Integer, primary_key=True)
    subject_type = Column(String(16), index=True)  # carrier|driver|shipper
    subject_id = Column(String(50), index=True)
    rater_id = Column(String(50))
    shipment_id = Column(String(50))
    score = Column(Float)  # 1..5
    comment = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "subject_type": self.subject_type, "subject_id": self.subject_id,
                "rater_id": self.rater_id, "shipment_id": self.shipment_id, "score": self.score,
                "comment": self.comment, "created_at": self.created_at.isoformat() if self.created_at else None}


# --- telematics ----------------------------------------------------------------

class TelematicsPing(Base):
    __tablename__ = "telematics_pings"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(String(50), index=True)
    shipment_id = Column(String(50))
    lat = Column(Float)
    lon = Column(Float)
    speed_kmh = Column(Float)
    heading = Column(Float)
    odometer_km = Column(Float)
    fuel_level = Column(Float)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"vehicle_id": self.vehicle_id, "shipment_id": self.shipment_id, "lat": self.lat,
                "lon": self.lon, "speed_kmh": self.speed_kmh, "heading": self.heading,
                "odometer_km": self.odometer_km, "fuel_level": self.fuel_level,
                "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None}


class TelematicsEvent(Base):
    __tablename__ = "telematics_events"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(String(50), index=True)
    event_type = Column(String(24))  # harsh_braking|speeding|geofence|tamper|offline|online|health
    severity = Column(String(12))
    data = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "vehicle_id": self.vehicle_id, "event_type": self.event_type,
                "severity": self.severity, "data": self.data,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class DutyLog(Base):
    """Driver hours-of-service duty status segments."""

    __tablename__ = "duty_logs"

    id = Column(Integer, primary_key=True)
    driver_id = Column(String(50), index=True)
    status = Column(String(12))  # driving|on_duty|rest|off
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime)

    def to_dict(self):
        return {"id": self.id, "driver_id": self.driver_id, "status": self.status,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "ended_at": self.ended_at.isoformat() if self.ended_at else None}


# --- payments / escrow / ePOD --------------------------------------------------

class Escrow(Base):
    __tablename__ = "escrows"

    id = Column(Integer, primary_key=True)
    escrow_id = Column(String(50), unique=True, nullable=False, index=True)
    tenant_id = Column(String(50), index=True)
    shipment_id = Column(String(50), index=True)
    load_ref = Column(String(50))
    payer_id = Column(String(50))
    payee_id = Column(String(50))
    amount = Column(Float)
    currency = Column(String(8), default="USD")
    status = Column(String(16), default="pending")  # pending|funded|released|refunded|disputed
    provider = Column(String(40))
    provider_ref = Column(String(120))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"escrow_id": self.escrow_id, "shipment_id": self.shipment_id, "load_ref": self.load_ref,
                "payer_id": self.payer_id, "payee_id": self.payee_id, "amount": self.amount,
                "currency": self.currency, "status": self.status, "provider": self.provider,
                "provider_ref": self.provider_ref,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None}


class ProofOfDelivery(Base):
    __tablename__ = "proofs_of_delivery"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(String(50), index=True)
    recipient_name = Column(String(120))
    signature_ref = Column(String(200))
    photo_refs = Column(JSON, default=list)
    notes = Column(String(500))
    delivered_at = Column(String(40))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "shipment_id": self.shipment_id, "recipient_name": self.recipient_name,
                "signature_ref": self.signature_ref, "photo_refs": self.photo_refs or [],
                "notes": self.notes, "delivered_at": self.delivered_at,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class Settlement(Base):
    __tablename__ = "settlements"

    id = Column(Integer, primary_key=True)
    escrow_id = Column(String(50), index=True)
    gross = Column(Float)
    tax_total = Column(Float)
    withheld = Column(Float)
    net_to_payee = Column(Float)
    currency = Column(String(8))
    breakdown = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "escrow_id": self.escrow_id, "gross": self.gross,
                "tax_total": self.tax_total, "withheld": self.withheld,
                "net_to_payee": self.net_to_payee, "currency": self.currency, "breakdown": self.breakdown,
                "created_at": self.created_at.isoformat() if self.created_at else None}


# --- insurance -----------------------------------------------------------------

class InsuranceRate(Base):
    """Operator/insurer-configured cargo insurance rate (no built-in rates)."""

    __tablename__ = "insurance_rates"

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    insurer_code = Column(String(50))
    region_code = Column(String(8), index=True)
    level = Column(String(16))  # basic|premium
    rate_percent = Column(Float)
    min_premium = Column(Float, default=0.0)
    active = Column(Boolean, default=True)

    def to_dict(self):
        return {"code": self.code, "insurer_code": self.insurer_code, "region_code": self.region_code,
                "level": self.level, "rate_percent": self.rate_percent, "min_premium": self.min_premium,
                "active": self.active}


class InsurancePolicy(Base):
    __tablename__ = "insurance_policies"

    id = Column(Integer, primary_key=True)
    policy_id = Column(String(50), unique=True, nullable=False, index=True)
    load_ref = Column(String(50), index=True)
    shipment_id = Column(String(50))
    insurer_code = Column(String(50))
    level = Column(String(16))
    sum_insured = Column(Float)
    premium = Column(Float)
    currency = Column(String(8))
    status = Column(String(16), default="quoted")  # quoted|bound|cancelled|claimed
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"policy_id": self.policy_id, "load_ref": self.load_ref, "shipment_id": self.shipment_id,
                "insurer_code": self.insurer_code, "level": self.level, "sum_insured": self.sum_insured,
                "premium": self.premium, "currency": self.currency, "status": self.status,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class InsuranceClaim(Base):
    __tablename__ = "insurance_claims"

    id = Column(Integer, primary_key=True)
    claim_id = Column(String(50), unique=True, nullable=False, index=True)
    policy_id = Column(String(50), index=True)
    shipment_id = Column(String(50))
    reason = Column(String(200))
    amount = Column(Float)
    status = Column(String(16), default="open")  # open|review|approved|rejected|paid
    evidence = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"claim_id": self.claim_id, "policy_id": self.policy_id, "shipment_id": self.shipment_id,
                "reason": self.reason, "amount": self.amount, "status": self.status,
                "evidence": self.evidence or [],
                "created_at": self.created_at.isoformat() if self.created_at else None}


# --- resilience: audit & idempotency ------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(String(80))
    tenant_id = Column(String(50))
    method = Column(String(10))
    path = Column(String(300))
    status_code = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "actor": self.actor, "tenant_id": self.tenant_id, "method": self.method,
                "path": self.path, "status_code": self.status_code,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    id = Column(Integer, primary_key=True)
    key = Column(String(120), unique=True, nullable=False, index=True)
    method = Column(String(10))
    path = Column(String(300))
    status_code = Column(Integer)
    response_json = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)


class KycCheck(Base):
    """Result of an automated document-verification / screening run."""

    __tablename__ = "kyc_checks"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, index=True)
    entity_type = Column(String(20))
    entity_id = Column(String(50))
    provider = Column(String(40))
    kind = Column(String(20))   # document|screening
    verified = Column(Boolean)
    status = Column(String(40))
    details = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "document_id": self.document_id, "entity_type": self.entity_type,
                "entity_id": self.entity_id, "provider": self.provider, "kind": self.kind,
                "verified": self.verified, "status": self.status, "details": self.details,
                "created_at": self.created_at.isoformat() if self.created_at else None}
