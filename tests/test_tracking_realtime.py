"""Tests for SSE push and TRAAS-driven reroute in prisaMove tracking."""

from datetime import datetime
from queue import Empty

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base


class FakeTraas:
    """Stand-in for the TRAAS routing service."""

    def __init__(self, distance_km):
        self.distance_km = distance_km
        self.added_hazards = []
        self.optimized = False

    def add_hazard(self, hazard_type, location, severity="High", radius_km=3.0):
        self.added_hazards.append((hazard_type, location, radius_km))
        return "hz_fake"

    def optimize(self, origin, destination, vehicle_type="agricultural_vehicle", hazards=None):
        self.optimized = True
        return {"distance_km": self.distance_km, "duration_min": self.distance_km / 50 * 60,
                "path": [origin, destination]}


def _make(traas=None):
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(
        settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False),
        session_factory=factory, traas_client=traas,
    )
    app.testing = True
    return app


def _award(client):
    load = client.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 8000,
        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    offer = client.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "t1", "bidder_role": "transporter", "price": 1200}).get_json()
    return client.post(f"/api/offers/{offer['id']}/accept", json={}).get_json()["shipment"]


def _eta(s):
    return datetime.fromisoformat(s["eta"])


# --- TRAAS reroute -------------------------------------------------------------

def test_reroute_uses_traas_distance_and_pushes_eta():
    traas = FakeTraas(distance_km=1500)
    app = _make(traas)
    client = app.test_client()
    shipment = _award(client)
    sid = shipment["shipment_id"]
    before = _eta(shipment)
    res = client.post(f"/api/shipments/{sid}/reroute", json={
        "reason_code": "reroute_flooding",
        "hazard": {"lat": 11.0, "lon": 9.0, "radius_km": 5}}).get_json()
    assert traas.optimized is True
    assert traas.added_hazards and traas.added_hazards[0][0] == "flood"  # reason -> hazard class
    assert res["planned_distance_km"] == 1500
    assert res["route_factor"] > 1.0
    assert _eta(res) > before  # longer avoidance route -> later ETA
    events = client.get(f"/api/shipments/{sid}/events").get_json()["events"]
    reroute_ev = [e for e in events if e["event_type"] == "reroute"][0]
    assert reroute_ev["data"]["routed_via"] == "traas"


def test_reroute_route_factor_carries_forward():
    app = _make(FakeTraas(distance_km=1500))
    client = app.test_client()
    sid = _award(client)["shipment_id"]
    client.post(f"/api/shipments/{sid}/reroute", json={"reason_code": "reroute_flooding",
                "hazard": {"lat": 11.0, "lon": 9.0}})
    # A later GPS ping keeps the detour penalty (route_factor stays applied).
    after = client.post(f"/api/shipments/{sid}/location", json={"lat": 11.0, "lon": 10.0}).get_json()
    assert after["route_factor"] and after["route_factor"] > 1.2


def test_reroute_explicit_distance_marks_operator():
    app = _make(traas=None)  # no TRAAS configured
    client = app.test_client()
    sid = _award(client)["shipment_id"]
    res = client.post(f"/api/shipments/{sid}/reroute",
                      json={"reason_code": "reroute_security", "new_distance_km": 900}).get_json()
    assert res["planned_distance_km"] == 900
    ev = [e for e in client.get(f"/api/shipments/{sid}/events").get_json()["events"]
          if e["event_type"] == "reroute"][0]
    assert ev["data"]["routed_via"] == "operator"


# --- SSE push ------------------------------------------------------------------

def test_event_bus_receives_shipment_events():
    app = _make()
    client = app.test_client()
    sid = _award(client)["shipment_id"]
    bus = app.config["EVENT_BUS"]
    q = bus.subscribe(f"shipment:{sid}")
    # An action should publish an event to the bus (what SSE streams).
    client.post(f"/api/shipments/{sid}/location", json={"lat": 11.0, "lon": 10.0, "speed_kmh": 50})
    event = q.get(timeout=2)
    assert event["event_type"] == "location"
    assert event["location"] == {"lat": 11.0, "lon": 10.0}


def test_sse_endpoint_streams_snapshot():
    app = _make()
    client = app.test_client()
    sid = _award(client)["shipment_id"]
    resp = client.get(f"/api/shipments/{sid}/stream", buffered=False)
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    it = resp.response
    chunks = [next(it), next(it)]  # retry directive + snapshot event
    resp.close()  # triggers unsubscribe
    blob = "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)
    assert "event: snapshot" in blob
    assert sid in blob


def test_sse_unknown_shipment_404():
    app = _make()
    assert app.test_client().get("/api/shipments/nope/stream").status_code == 404
