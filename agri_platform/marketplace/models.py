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
    status = Column(String(20), default="open")  # open|negotiating|awarded|in_transit|delivered|cancelled
    awarded_offer_id = Column(Integer)
    notes = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "ref": self.ref,
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
    owner_id = Column(String(50), index=True)
    vehicle_type = Column(String(40))  # pickup|truck_small|truck_large|refrigerated_truck|flatbed|tanker|livestock_carrier
    capacity_kg = Column(Float)
    capacity_m3 = Column(Float)
    features = Column(JSON, default=list)  # [refrigeration, crane, livestock_rated, hazmat_certified, gps]
    base_rate_per_km = Column(Float)
    location = Column(JSON)
    available = Column(String(10), default="true")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "vehicle_id": self.vehicle_id,
            "owner_id": self.owner_id,
            "vehicle_type": self.vehicle_type,
            "capacity_kg": self.capacity_kg,
            "capacity_m3": self.capacity_m3,
            "features": self.features or [],
            "base_rate_per_km": self.base_rate_per_km,
            "location": self.location,
            "available": self.available,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Offer(Base):
    """A rate offer (bid) placed against a load."""

    __tablename__ = "offers"

    id = Column(Integer, primary_key=True)
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
