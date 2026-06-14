"""prisaMove shipment tracking: live location, ETA, delays and reroutes.

ETA is computed from the *driver-reported* current location to the destination
(real input, not a simulation): remaining road distance / average speed, plus any
accumulated delay. Delay and reroute events carry a reason selected from a
pre-seeded, admin-extensible catalog. Events are appended to the shipment's
timeline and dispatched to an optional notifier (console log by default, or a
webhook for the shipper/operator).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Protocol

import requests

from ..common import regions
from ..common.geo import haversine_km
from .models import DelayReason, Shipment, ShipmentEvent, TrackPoint

logger = logging.getLogger(__name__)

ROAD_DISTANCE_FACTOR = 1.3
DEFAULT_AVG_SPEED_KMH = 50.0

# Pre-seeded reason catalog (operational categories; admins can add more).
DEFAULT_REASONS = [
    # delay
    ("traffic_congestion", "Traffic congestion", "delay"),
    ("road_accident", "Road accident", "delay"),
    ("vehicle_breakdown", "Vehicle breakdown", "delay"),
    ("adverse_weather", "Adverse weather", "delay"),
    ("flooding", "Flooding", "delay"),
    ("security_checkpoint", "Security checkpoint", "delay"),
    ("road_closure", "Road closure", "delay"),
    ("border_delay", "Border / customs delay", "delay"),
    ("loading_delay", "Loading / offloading delay", "delay"),
    ("documentation_issue", "Documentation issue", "delay"),
    ("fuel_shortage", "Fuel shortage", "delay"),
    ("driver_rest", "Mandatory driver rest", "delay"),
    ("customer_unavailable", "Customer unavailable", "delay"),
    # reroute
    ("reroute_road_closure", "Road closure ahead", "reroute"),
    ("reroute_flooding", "Flooded road", "reroute"),
    ("reroute_security", "Security risk / red zone", "reroute"),
    ("reroute_accident", "Accident ahead", "reroute"),
    ("reroute_weather", "Weather hazard", "reroute"),
    ("reroute_bridge", "Bridge closure", "reroute"),
    ("reroute_faster", "Faster alternative route", "reroute"),
]


# --- notifier ------------------------------------------------------------------

class TrackingNotifier(Protocol):
    def dispatch(self, shipment: Shipment, event: ShipmentEvent) -> bool: ...


class ConsoleTrackingNotifier:
    def __init__(self):
        self.sent: List[dict] = []

    def dispatch(self, shipment, event) -> bool:
        payload = {"shipment_id": shipment.shipment_id, "event": event.event_type,
                   "status": shipment.status, "eta": shipment.eta, "reason": event.reason_label}
        self.sent.append(payload)
        logger.info("tracking notify %s", payload)
        return True


# Event types worth an outbound webhook (location pings are high-frequency and
# stream over SSE instead).
WEBHOOK_EVENTS = {"status", "delay", "reroute", "eta"}


class WebhookTrackingNotifier:
    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout
        self._fallback = ConsoleTrackingNotifier()

    def dispatch(self, shipment, event) -> bool:
        if event.event_type not in WEBHOOK_EVENTS:
            return True
        try:
            requests.post(self.url, json={"shipment": shipment.to_dict(), "event": event.to_dict()},
                          timeout=self.timeout)
            return True
        except requests.RequestException as exc:
            logger.warning("tracking webhook failed: %s", exc)
            return self._fallback.dispatch(shipment, event)


class BusTrackingNotifier:
    """Publishes every event to an in-process EventBus for SSE streaming."""

    def __init__(self, bus, channel_prefix: str = "shipment:"):
        self._bus = bus
        self._prefix = channel_prefix

    def dispatch(self, shipment, event) -> bool:
        self._bus.publish(self._prefix + shipment.shipment_id, event.to_dict())
        return True


class CompositeNotifier:
    """Fans an event out to several notifiers (e.g. bus + webhook)."""

    def __init__(self, notifiers):
        self._notifiers = [n for n in notifiers if n is not None]

    def dispatch(self, shipment, event) -> bool:
        ok = True
        for n in self._notifiers:
            try:
                ok = n.dispatch(shipment, event) and ok
            except Exception:  # pragma: no cover - a sink must not break tracking
                logger.exception("tracking notifier failed")
                ok = False
        return ok


def make_tracking_notifier(settings) -> TrackingNotifier:
    if getattr(settings, "tracking_webhook_url", None):
        return WebhookTrackingNotifier(settings.tracking_webhook_url)
    return ConsoleTrackingNotifier()


# --- reasons -------------------------------------------------------------------

def seed_reasons(session) -> int:
    """Insert default reasons that are not already present. Idempotent."""
    existing = {r.code for r in session.query(DelayReason).all()}
    added = 0
    for code, label, category in DEFAULT_REASONS:
        if code not in existing:
            session.add(DelayReason(code=code, label=label, category=category, active=True))
            added += 1
    if added:
        session.commit()
    return added


def list_reasons(session, category: Optional[str] = None) -> List[dict]:
    seed_reasons(session)
    q = session.query(DelayReason).filter(DelayReason.active.is_(True))
    if category:
        q = q.filter(DelayReason.category == category)
    return [r.to_dict() for r in q.order_by(DelayReason.category, DelayReason.code).all()]


_DEFAULT_REASON_LABELS = {code: label for code, label, _ in DEFAULT_REASONS}


def _reason_label(session, code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    row = session.query(DelayReason).filter_by(code=code).first()
    if row:
        return row.label
    return _DEFAULT_REASON_LABELS.get(code, code)


# --- ETA -----------------------------------------------------------------------

def remaining_km(shipment: Shipment) -> float:
    point = shipment.current_location or shipment.origin
    dest = shipment.destination
    if not point or not dest:
        return 0.0
    straight = haversine_km(point["lat"], point["lon"], dest["lat"], dest["lon"])
    # A reroute may have set a detour factor (rerouted distance / straight line);
    # otherwise use the baseline road factor.
    factor = shipment.route_factor or ROAD_DISTANCE_FACTOR
    return straight * factor


def compute_eta(shipment: Shipment, now: Optional[datetime] = None) -> str:
    now = now or datetime.utcnow()
    if shipment.status in ("delivered", "arrived", "cancelled"):
        return now.isoformat()
    speed = shipment.avg_speed_kmh or DEFAULT_AVG_SPEED_KMH
    minutes = (remaining_km(shipment) / max(1.0, speed)) * 60.0 + (shipment.delay_minutes or 0.0)
    return (now + timedelta(minutes=minutes)).isoformat()


# --- lifecycle -----------------------------------------------------------------

def _event(session, shipment, notifier, **kwargs) -> ShipmentEvent:
    event = ShipmentEvent(shipment_id=shipment.shipment_id, **kwargs)
    session.add(event)
    session.flush()
    # Every event is dispatched; each notifier decides what to do with it
    # (the bus streams all of them, the webhook filters to important ones).
    if notifier is not None:
        notifier.dispatch(shipment, event)
    return event


def create_shipment(session, load, offer, *, driver_name=None, driver_phone=None,
                    avg_speed_kmh=None, notifier=None) -> Shipment:
    shipment = Shipment(
        shipment_id=f"shp_{uuid.uuid4().hex[:10]}",
        load_ref=load.ref,
        offer_id=offer.id,
        carrier_id=offer.bidder_id,
        vehicle_id=offer.vehicle_id,
        driver_name=driver_name,
        driver_phone=driver_phone,
        region_code=(lambda r: r.code if r else None)(regions.region_for_coords(load.origin)),
        origin=load.origin,
        destination=load.destination,
        current_location=load.origin,
        status="assigned",
        planned_distance_km=round(
            haversine_km(load.origin["lat"], load.origin["lon"],
                         load.destination["lat"], load.destination["lon"]) * ROAD_DISTANCE_FACTOR, 2
        ) if load.origin and load.destination else None,
        avg_speed_kmh=avg_speed_kmh or DEFAULT_AVG_SPEED_KMH,
        delay_minutes=0.0,
        currency=load.currency,
        price=offer.price,
    )
    shipment.eta = compute_eta(shipment)
    shipment.last_update_at = datetime.utcnow()
    session.add(shipment)
    session.flush()
    _event(session, shipment, notifier, event_type="status", status="assigned",
           message="Shipment created and assigned to carrier", eta=shipment.eta)
    session.commit()
    return shipment


def add_location(session, shipment, lat, lon, *, speed_kmh=None, heading=None,
                 source="driver", notifier=None) -> Shipment:
    now = datetime.utcnow()
    session.add(TrackPoint(shipment_id=shipment.shipment_id, lat=lat, lon=lon,
                           speed_kmh=speed_kmh, heading=heading, source=source, recorded_at=now))
    shipment.current_location = {"lat": lat, "lon": lon}
    if speed_kmh:
        # blend reported speed into the running average for a steadier ETA
        prev = shipment.avg_speed_kmh or DEFAULT_AVG_SPEED_KMH
        shipment.avg_speed_kmh = round(prev * 0.7 + float(speed_kmh) * 0.3, 1)
    if shipment.status in ("assigned", "picked_up"):
        shipment.status = "en_route"
    shipment.eta = compute_eta(shipment, now)
    shipment.last_update_at = now
    shipment.updated_at = now
    _event(session, shipment, notifier, event_type="location", status=shipment.status,
           location=shipment.current_location, eta=shipment.eta,
           data={"speed_kmh": speed_kmh, "remaining_km": round(remaining_km(shipment), 2)})
    session.commit()
    return shipment


VALID_STATUSES = {"assigned", "picked_up", "en_route", "delayed", "arrived", "delivered", "cancelled"}


def update_status(session, shipment, status, *, message=None, created_by=None, notifier=None) -> Shipment:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status '{status}'")
    shipment.status = status
    shipment.eta = compute_eta(shipment)
    shipment.updated_at = datetime.utcnow()
    _event(session, shipment, notifier, event_type="status", status=status,
           message=message or f"Status updated to {status}", eta=shipment.eta, created_by=created_by)
    session.commit()
    return shipment


def report_delay(session, shipment, *, delay_minutes, reason_code=None, message=None,
                 created_by=None, notifier=None) -> Shipment:
    label = _reason_label(session, reason_code)
    shipment.delay_minutes = (shipment.delay_minutes or 0.0) + float(delay_minutes)
    if shipment.status not in ("delivered", "arrived", "cancelled"):
        shipment.status = "delayed"
    shipment.eta = compute_eta(shipment)
    shipment.updated_at = datetime.utcnow()
    _event(session, shipment, notifier, event_type="delay", status=shipment.status,
           reason_code=reason_code, reason_label=label, delay_minutes=float(delay_minutes),
           message=message or f"Delay reported: {label or 'unspecified'}", eta=shipment.eta,
           created_by=created_by)
    session.commit()
    return shipment


def reroute(session, shipment, *, reason_code=None, message=None, new_destination=None,
            new_distance_km=None, waypoints=None, hazard=None, traas_client=None,
            created_by=None, notifier=None) -> Shipment:
    """Reroute a shipment around a hazard and recompute the ETA.

    Distance precedence: an explicit ``new_distance_km`` wins; otherwise, if a
    TRAAS client is available, ask it for a hazard-avoiding route from the current
    position and use that distance; otherwise leave the estimate unchanged. The
    rerouted distance is converted into a ``route_factor`` so the detour penalty
    carries forward into the ETA as the driver continues to report positions.
    """
    from .routing_client import REASON_TO_HAZARD, RoutingClientError

    label = _reason_label(session, reason_code)
    if new_destination:
        shipment.destination = new_destination

    point = shipment.current_location or shipment.origin
    dest = shipment.destination
    straight = haversine_km(point["lat"], point["lon"], dest["lat"], dest["lon"]) if point and dest else 0.0

    distance = float(new_distance_km) if new_distance_km is not None else None
    routed_via = "operator" if distance is not None else None
    if distance is None and traas_client is not None and point and dest:
        try:
            if hazard and hazard.get("lat") is not None and hazard.get("lon") is not None:
                traas_client.add_hazard(
                    REASON_TO_HAZARD.get(reason_code, "safety"),
                    {"lat": hazard["lat"], "lon": hazard["lon"]},
                    radius_km=float(hazard.get("radius_km", 3.0)),
                )
            route = traas_client.optimize([point["lat"], point["lon"]], [dest["lat"], dest["lon"]])
            if route.get("distance_km"):
                distance = float(route["distance_km"])
                waypoints = waypoints or route.get("path")
                routed_via = "traas"
        except RoutingClientError as exc:
            logger.warning("reroute via TRAAS failed, keeping estimate: %s", exc)

    if distance is not None:
        shipment.planned_distance_km = distance
        if straight > 0:
            shipment.route_factor = max(1.0, distance / straight)

    if shipment.status not in ("delivered", "arrived", "cancelled"):
        shipment.status = "en_route"
    shipment.eta = compute_eta(shipment)
    shipment.updated_at = datetime.utcnow()
    _event(session, shipment, notifier, event_type="reroute", status=shipment.status,
           reason_code=reason_code, reason_label=label,
           message=message or f"Rerouted: {label or 'unspecified'}", eta=shipment.eta,
           location=shipment.current_location,
           data={"waypoints": waypoints, "planned_distance_km": shipment.planned_distance_km,
                 "routed_via": routed_via}, created_by=created_by)
    session.commit()
    return shipment
