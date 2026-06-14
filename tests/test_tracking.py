"""Tests for prisaMove shipment tracking, ETA, delays and reroutes."""

from datetime import datetime

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base
from agri_platform.marketplace.tracking import ConsoleTrackingNotifier


@pytest.fixture
def ctx():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    notifier = ConsoleTrackingNotifier()
    app = create_app(
        settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False),
        session_factory=factory, tracking_notifier=notifier,
    )
    app.testing = True
    return app.test_client(), notifier


def _award(client):
    load = client.post("/api/loads", json={
        "shipper_id": "s1", "title": "Maize", "weight_kg": 8000,
        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    offer = client.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "t1", "bidder_role": "transporter", "price": 1200}).get_json()
    res = client.post(f"/api/offers/{offer['id']}/accept",
                      json={"driver_name": "Musa", "driver_phone": "+234..."}).get_json()
    return load, res["shipment"]


def _eta(s):
    return datetime.fromisoformat(s["eta"])


def test_accept_creates_shipment_with_eta(ctx):
    client, _ = ctx
    _, shipment = _award(client)
    assert shipment is not None
    assert shipment["status"] == "assigned"
    assert shipment["eta"] and shipment["planned_distance_km"] > 0
    assert shipment["driver_name"] == "Musa"
    assert shipment["region_code"] == "NG"


def test_reasons_seeded_and_categorised(ctx):
    client, _ = ctx
    reasons = client.get("/api/tracking/reasons").get_json()["reasons"]
    codes = {r["code"] for r in reasons}
    assert "traffic_congestion" in codes
    cats = {r["category"] for r in reasons}
    assert {"delay", "reroute"} <= cats
    reroute_only = client.get("/api/tracking/reasons?category=reroute").get_json()["reasons"]
    assert all(r["category"] == "reroute" for r in reroute_only)


def test_location_update_advances_and_moves_toward_dest(ctx):
    client, notifier = ctx
    _, shipment = _award(client)
    sid = shipment["shipment_id"]
    before = _eta(shipment)
    # Driver reports a position closer to the destination.
    updated = client.post(f"/api/shipments/{sid}/location",
                          json={"lat": 11.5, "lon": 12.5, "speed_kmh": 60}).get_json()
    assert updated["status"] == "en_route"
    assert updated["current_location"] == {"lat": 11.5, "lon": 12.5}
    # closer -> ETA sooner than the origin-based estimate
    assert _eta(updated) < before
    track = client.get(f"/api/shipments/{sid}/track").get_json()["track"]
    assert len(track) == 1 and track[0]["lat"] == 11.5


def test_delay_pushes_eta_and_logs_reason(ctx):
    client, notifier = ctx
    _, shipment = _award(client)
    sid = shipment["shipment_id"]
    before = _eta(shipment)
    res = client.post(f"/api/shipments/{sid}/delay",
                      json={"delay_minutes": 90, "reason_code": "security_checkpoint"}).get_json()
    assert res["status"] == "delayed"
    assert res["delay_minutes"] == 90
    assert _eta(res) > before
    events = client.get(f"/api/shipments/{sid}/events").get_json()["events"]
    delay_events = [e for e in events if e["event_type"] == "delay"]
    assert delay_events and delay_events[0]["reason_label"] == "Security checkpoint"
    # notifier received the delay
    assert any(n["event"] == "delay" for n in notifier.sent)


def test_reroute_records_reason_and_distance(ctx):
    client, _ = ctx
    _, shipment = _award(client)
    sid = shipment["shipment_id"]
    res = client.post(f"/api/shipments/{sid}/reroute", json={
        "reason_code": "reroute_flooding", "new_distance_km": 600,
        "new_destination": {"lat": 11.9, "lon": 13.2}}).get_json()
    assert res["planned_distance_km"] == 600
    events = client.get(f"/api/shipments/{sid}/events").get_json()["events"]
    assert any(e["event_type"] == "reroute" and e["reason_label"] == "Flooded road" for e in events)


def test_status_delivered_sets_eta_now(ctx):
    client, _ = ctx
    _, shipment = _award(client)
    sid = shipment["shipment_id"]
    res = client.post(f"/api/shipments/{sid}/status", json={"status": "delivered"}).get_json()
    assert res["status"] == "delivered"
    # ETA collapses to ~now (delivered)
    assert (datetime.utcnow() - _eta(res)).total_seconds() < 5


def test_invalid_status_rejected(ctx):
    client, _ = ctx
    _, shipment = _award(client)
    r = client.post(f"/api/shipments/{shipment['shipment_id']}/status", json={"status": "teleported"})
    assert r.status_code == 422


def test_admin_add_custom_reason():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False,
                     admin_api_keys=frozenset({"k"})), session_factory=factory)
    c = app.test_client()
    assert c.post("/api/admin/tracking-reasons", json={"code": "x", "label": "X"}).status_code == 403
    r = c.post("/api/admin/tracking-reasons", headers={"X-Admin-Key": "k"},
               json={"code": "elephant_crossing", "label": "Elephant crossing", "category": "delay"})
    assert r.status_code == 201
    codes = {x["code"] for x in c.get("/api/tracking/reasons").get_json()["reasons"]}
    assert "elephant_crossing" in codes
