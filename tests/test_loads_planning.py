"""Tests for load classification, vehicle matching and consolidation."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.marketplace import loads_planning
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base, Load


# --- pure scope ----------------------------------------------------------------

def test_classify_scope_local():
    assert loads_planning.classify_scope("NG", "NG", 20) == "local"


def test_classify_scope_intra_region():
    assert loads_planning.classify_scope("NG", "NG", 600) == "intra_region"


def test_classify_scope_inter_region():
    assert loads_planning.classify_scope("NG", "KE", 4000) == "inter_region"


# --- pure matching -------------------------------------------------------------

def _load(weight=5000, classifications=None):
    return Load(ref="l1", weight_kg=weight, classifications=classifications or [])


def test_match_excludes_too_small_and_ranks_efficient():
    load = _load(5000)
    vehicles = [
        {"vehicle_id": "small", "capacity_kg": 3000, "available": "true", "compliance_status": "approved", "features": []},
        {"vehicle_id": "big", "capacity_kg": 20000, "available": "true", "compliance_status": "approved", "features": []},
        {"vehicle_id": "fit", "capacity_kg": 6000, "available": "true", "compliance_status": "approved", "features": []},
    ]
    matches = loads_planning.match_vehicles(load, vehicles)
    ids = [m["vehicle_id"] for m in matches]
    assert "small" not in ids
    assert ids[0] == "fit"  # smallest sufficient capacity first


def test_match_requires_features_for_classifications():
    load = _load(2000, ["refrigerated"])
    vehicles = [
        {"vehicle_id": "plain", "capacity_kg": 8000, "available": "true", "compliance_status": "approved", "features": []},
        {"vehicle_id": "reefer", "capacity_kg": 8000, "available": "true", "compliance_status": "approved", "features": ["refrigeration"]},
    ]
    ids = [m["vehicle_id"] for m in loads_planning.match_vehicles(load, vehicles)]
    assert ids == ["reefer"]


def test_match_excludes_unapproved_when_required():
    load = _load(2000)
    vehicles = [{"vehicle_id": "v", "capacity_kg": 8000, "available": "true", "compliance_status": "pending", "features": []}]
    assert loads_planning.match_vehicles(load, vehicles, require_approved=True) == []
    assert len(loads_planning.match_vehicles(load, vehicles, require_approved=False)) == 1


# --- pure consolidation --------------------------------------------------------

def test_group_ltl_groups_same_corridor():
    loads = [
        Load(ref="a", weight_kg=1000, origin_region="NG", destination_region="GH"),
        Load(ref="b", weight_kg=1500, origin_region="NG", destination_region="GH"),
        Load(ref="c", weight_kg=2000, origin_region="NG", destination_region="KE"),  # alone -> not grouped
    ]
    groups = loads_planning.group_ltl(loads)
    assert len(groups) == 1
    g = groups[0]
    assert set(g["load_refs"]) == {"a", "b"} and g["total_weight_kg"] == 2500


def test_group_ltl_respects_max_weight():
    loads = [Load(ref=f"l{i}", weight_kg=4000, origin_region="NG", destination_region="GH") for i in range(3)]
    groups = loads_planning.group_ltl(loads, max_group_weight_kg=9000)
    # 4000*3 = 12000 over a 9000 cap -> first two grouped, third alone (dropped, <2)
    assert len(groups) == 1 and len(groups[0]["load_refs"]) == 2


# --- HTTP flow -----------------------------------------------------------------

@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False),
                     session_factory=factory)
    app.testing = True
    return app.test_client()


def test_load_gets_scope_and_mode(client):
    load = client.post("/api/loads", json={"shipper_id": "s", "weight_kg": 5000, "load_mode": "ltl",
                       "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 10.6, "lon": 7.5}}).get_json()
    assert load["load_mode"] == "ltl"
    assert load["scope"] == "local"  # ~15km, same region
    assert load["origin_region"] == "NG" and load["destination_region"] == "NG"


def test_inter_region_scope(client):
    load = client.post("/api/loads", json={"shipper_id": "s", "weight_kg": 5000,
                       "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": -1.29, "lon": 36.82}}).get_json()
    assert load["scope"] == "inter_region"
    assert load["destination_region"] == "KE"


def test_match_vehicles_endpoint(client):
    client.post("/api/vehicles", json={"vehicle_id": "v1", "owner_id": "o", "vehicle_type": "truck_large",
                "capacity_kg": 9000})
    load = client.post("/api/loads", json={"shipper_id": "s", "weight_kg": 5000,
                       "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    body = client.get(f"/api/loads/{load['ref']}/match-vehicles").get_json()
    assert any(m["vehicle_id"] == "v1" for m in body["matches"])


def test_consolidation_flow(client):
    refs = []
    for i in range(2):
        r = client.post("/api/loads", json={"shipper_id": "s", "weight_kg": 1500, "load_mode": "ltl",
                        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 6.5, "lon": 3.3}}).get_json()
        refs.append(r["ref"])
    suggestions = client.get("/api/consolidations/suggest").get_json()["groups"]
    assert suggestions and len(suggestions[0]["load_refs"]) == 2
    con = client.post("/api/consolidations", json={"load_refs": refs}).get_json()
    assert con["total_weight_kg"] == 3000
    # member loads now reference the consolidation
    load = client.get(f"/api/loads/{refs[0]}").get_json()
    assert load["consolidation_id"] == con["consolidation_id"] and load["load_mode"] == "consolidation"


def test_optional_insurance_stored(client):
    load = client.post("/api/loads", json={"shipper_id": "s", "weight_kg": 5000,
                       "insurance_opted": True, "insurance_level": "premium", "insurance_value": 500000,
                       "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    assert load["insurance_opted"] is True and load["insurance_level"] == "premium"
    assert load["insurance_value"] == 500000
