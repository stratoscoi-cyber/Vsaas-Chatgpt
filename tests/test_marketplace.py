"""Tests for LGaaS (prisaMove): pricing, negotiation and the HTTP surface."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.marketplace import negotiation, pricing, service
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base


# --- pricing -------------------------------------------------------------------

def test_chargeable_weight_uses_volumetric_when_bulky():
    # 1m x 1m x 1m = 1,000,000 cm3 / 4000 = 250 kg volumetric
    cw = pricing.chargeable_weight_kg(100, {"length": 100, "width": 100, "height": 100})
    assert cw == 250.0


def test_chargeable_weight_uses_actual_when_dense():
    cw = pricing.chargeable_weight_kg(500, {"length": 50, "width": 50, "height": 50})
    assert cw == 500.0


def test_estimate_rate_surcharges_increase_price():
    common = dict(weight_kg=2000, distance_km=300, load_type="general")
    plain = pricing.estimate_rate(**common).recommended
    refrigerated = pricing.estimate_rate(classifications=["refrigerated"], **common).recommended
    assert refrigerated > plain


def test_estimate_rate_band_and_min_charge():
    est = pricing.estimate_rate(weight_kg=1, distance_km=1)
    assert est.recommended == pricing.MIN_CHARGE  # tiny load floored
    assert est.low < est.recommended < est.high


def test_road_distance_factor_applied():
    d = pricing.road_distance_km({"lat": 0, "lon": 0}, {"lat": 0, "lon": 1})
    straight = pricing.haversine_km(0, 0, 0, 1)
    assert abs(d - straight * pricing.ROAD_DISTANCE_FACTOR) < 1e-6


# --- negotiation ---------------------------------------------------------------

def test_decide_move_accepts_when_price_good_for_shipper():
    move = negotiation.decide_move(
        my_target=100, their_price=90, role="shipper", round_index=1,
        profile=negotiation.get_profile("direct"),
    )
    assert move.action == "accept"


def test_decide_move_counter_between_target_and_their_price():
    move = negotiation.decide_move(
        my_target=100, their_price=200, role="shipper", round_index=1,
        profile=negotiation.get_profile("consensus"),
    )
    assert move.action == "counter"
    assert 100 <= move.counter_price <= 200


def test_higher_intensity_concedes_less_early():
    args = dict(my_target=100, their_price=200, role="shipper", round_index=1)
    calm = negotiation.decide_move(profile=negotiation.get_profile("direct"), **args).counter_price
    intense = negotiation.decide_move(profile=negotiation.get_profile("market_bargaining"), **args).counter_price
    # The intense bargainer holds a lower (tougher) counter for the shipper.
    assert intense < calm


def test_bidder_role_wants_higher():
    move = negotiation.decide_move(
        my_target=200, their_price=100, role="bidder", round_index=1,
        profile=negotiation.get_profile("consensus"),
    )
    assert move.action == "counter"
    assert 100 <= move.counter_price <= 200


def test_template_composer_adapts_to_style():
    move = negotiation.decide_move(
        my_target=100, their_price=200, role="shipper", round_index=1,
        profile=negotiation.get_profile("relationship_first"),
    )
    msg = negotiation.TemplateComposer().compose(
        negotiation.get_profile("relationship_first"), "shipper", move, "USD",
        {"counterparty_name": "Mr Bello", "load_title": "Tomatoes"},
    )
    assert "family" in msg.lower() or "partnership" in msg.lower()  # relationship emphasis
    assert "USD" in msg


def test_build_custom_profile():
    p = negotiation.build_profile({"key": "mine", "name": "Mine", "haggle_intensity": 0.9})
    assert p.key == "mine" and p.haggle_intensity == 0.9


# --- HTTP surface --------------------------------------------------------------

@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(
        settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False),
        session_factory=factory,
    )
    app.testing = True
    return app.test_client()


def _make_load(client, **over):
    payload = {
        "shipper_id": "farmer-1", "title": "Maize", "weight_kg": 5000,
        "load_type": "grain",
        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1},
    }
    payload.update(over)
    return client.post("/api/loads", json=payload).get_json()


def test_health_brand(client):
    body = client.get("/health").get_json()
    assert body["service"] == "lgaas" and body["brand"] == "prisaMove"


def test_negotiation_styles_listed(client):
    body = client.get("/api/negotiation-styles").get_json()
    keys = {s["key"] for s in body["styles"]}
    assert {"direct", "relationship_first", "market_bargaining"} <= keys
    assert body["default"] == negotiation.DEFAULT_PROFILE_KEY


def test_create_load_and_estimate(client):
    load = _make_load(client)
    assert load["status"] == "open"
    est = client.post(f"/api/loads/{load['ref']}/estimate", json={}).get_json()
    assert est["estimate"]["recommended"] > 0


def test_offer_capacity_validation(client):
    load = _make_load(client, weight_kg=20000, classifications=["refrigerated"])
    client.post("/api/vehicles", json={
        "vehicle_id": "v1", "owner_id": "o1", "vehicle_type": "truck_small",
        "capacity_kg": 3000, "features": [],
    })
    # too small + missing refrigeration
    r = client.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "o1", "bidder_role": "vehicle_owner", "price": 900, "vehicle_id": "v1",
    })
    assert r.status_code == 422


def test_full_bid_and_award_flow(client):
    load = _make_load(client)
    r = client.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "trans-1", "bidder_role": "transporter", "price": 1200,
        "negotiation_style": "market_bargaining",
    })
    assert r.status_code == 201
    offer_id = r.get_json()["id"]
    # load is now negotiating
    assert client.get(f"/api/loads/{load['ref']}").get_json()["status"] == "negotiating"

    # AI haggle from the shipper's side
    h = client.post(f"/api/offers/{offer_id}/haggle", json={
        "as_role": "shipper", "negotiation_style": "market_bargaining",
        "counterparty_name": "Transporter A",
    }).get_json()
    assert h["move"]["action"] in ("accept", "counter")
    assert isinstance(h["message"], str) and h["message"]
    assert "fair_rate" in h

    # shipper counters
    c = client.post(f"/api/offers/{offer_id}/counter", json={
        "price": 950, "author_id": "farmer-1", "author_role": "shipper",
    })
    assert c.status_code == 200 and c.get_json()["round"] == 1

    # accept
    a = client.post(f"/api/offers/{offer_id}/accept")
    assert a.status_code == 200
    body = a.get_json()
    assert body["offer"]["status"] == "accepted"
    assert body["load"]["status"] == "awarded"

    # negotiation thread has entries (offer, haggle assistant, counter)
    msgs = client.get(f"/api/loads/{load['ref']}/messages").get_json()["messages"]
    roles = [m["author_role"] for m in msgs]
    assert "bidder" in roles and "assistant" in roles and "shipper" in roles


def test_accept_rejects_other_offers(client):
    load = _make_load(client)
    o1 = client.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "t1", "bidder_role": "transporter", "price": 1000}).get_json()["id"]
    client.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "t2", "bidder_role": "transporter", "price": 1100})
    client.post(f"/api/offers/{o1}/accept")
    offers = client.get(f"/api/loads/{load['ref']}/offers").get_json()["offers"]
    statuses = {o["bidder_id"]: o["status"] for o in offers}
    assert statuses["t1"] == "accepted" and statuses["t2"] == "rejected"


def test_adhoc_estimate(client):
    r = client.post("/api/estimate", json={
        "weight_kg": 10000, "distance_km": 500, "load_type": "perishable_produce",
        "classifications": ["refrigerated"],
    })
    assert r.status_code == 200 and r.get_json()["estimate"]["recommended"] > 0
