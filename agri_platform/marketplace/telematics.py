"""Telematics ingestion from the mandated IoT tracker.

Heartbeats carry real device positions and feed the live shipment track; safety
events (tamper, harsh braking, speeding, geofence) are recorded, and a
tamper/offline condition auto-suspends the vehicle so it cannot transact until an
operator clears it. Nothing is synthesised — these are device-reported readings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional

from . import tracking
from .models import Shipment, TelematicsEvent, TelematicsPing, Vehicle

OFFLINE_AFTER_MINUTES = 30
AUTO_SUSPEND_EVENTS = {"tamper"}


def _vehicle(session, vehicle_id) -> Optional[Vehicle]:
    return session.query(Vehicle).filter_by(vehicle_id=vehicle_id).first()


def heartbeat(session, vehicle_id: str, *, lat=None, lon=None, speed_kmh=None, heading=None,
              odometer_km=None, fuel_level=None, shipment_id=None, notifier=None) -> Dict:
    vehicle = _vehicle(session, vehicle_id)
    if vehicle is None:
        raise LookupError("vehicle not found")
    now = datetime.utcnow()
    session.add(TelematicsPing(vehicle_id=vehicle_id, shipment_id=shipment_id, lat=lat, lon=lon,
                               speed_kmh=speed_kmh, heading=heading, odometer_km=odometer_km,
                               fuel_level=fuel_level, recorded_at=now))
    vehicle.last_heartbeat_at = now
    if vehicle.telematics_status != "suspected_tamper":
        vehicle.telematics_status = "online"
    if odometer_km is not None:
        vehicle.odometer_km = odometer_km
    session.commit()

    # If on an active shipment, drive the live track from the device.
    if shipment_id and lat is not None and lon is not None:
        shipment = session.query(Shipment).filter_by(shipment_id=shipment_id).first()
        if shipment and shipment.status not in ("delivered", "cancelled"):
            tracking.add_location(session, shipment, lat, lon, speed_kmh=speed_kmh,
                                  heading=heading, source="gps", notifier=notifier)
    return vehicle.to_dict()


def record_event(session, vehicle_id: str, event_type: str, *, severity="info", data=None) -> Dict:
    vehicle = _vehicle(session, vehicle_id)
    if vehicle is None:
        raise LookupError("vehicle not found")
    session.add(TelematicsEvent(vehicle_id=vehicle_id, event_type=event_type, severity=severity, data=data))
    suspended = False
    if event_type in AUTO_SUSPEND_EVENTS:
        vehicle.telematics_status = "suspected_tamper"
        vehicle.compliance_status = "suspended"
        vehicle.available = "false"
        suspended = True
    session.commit()
    return {"vehicle_id": vehicle_id, "event_type": event_type, "auto_suspended": suspended,
            "vehicle_status": vehicle.compliance_status}


def reconcile_offline(session, *, as_of: Optional[datetime] = None) -> int:
    """Mark vehicles offline when their last heartbeat is stale. Returns count."""
    as_of = as_of or datetime.utcnow()
    cutoff = as_of - timedelta(minutes=OFFLINE_AFTER_MINUTES)
    n = 0
    for v in session.query(Vehicle).filter(Vehicle.telematics_status == "online").all():
        if v.last_heartbeat_at and v.last_heartbeat_at < cutoff:
            v.telematics_status = "offline"
            n += 1
    if n:
        session.commit()
    return n


def status(session, vehicle_id: str) -> Dict:
    vehicle = _vehicle(session, vehicle_id)
    if vehicle is None:
        raise LookupError("vehicle not found")
    events = (session.query(TelematicsEvent).filter_by(vehicle_id=vehicle_id)
              .order_by(TelematicsEvent.id.desc()).limit(20).all())
    return {"vehicle_id": vehicle_id, "telematics_status": vehicle.telematics_status,
            "last_heartbeat_at": vehicle.last_heartbeat_at.isoformat() if vehicle.last_heartbeat_at else None,
            "odometer_km": vehicle.odometer_km, "recent_events": [e.to_dict() for e in events]}
