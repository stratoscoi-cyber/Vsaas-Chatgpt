"""Tests for the TRAAS service logic and HTTP surface."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.traas import service
from agri_platform.traas.app import create_app
from agri_platform.traas.models import Base
from agri_platform.traas.routing import StraightLineEngine


# --- pure logic ----------------------------------------------------------------

def test_safety_score_decreases_with_hazards():
    clean = service.route_safety([])
    risky = service.route_safety([{"hazard_type": "safety"}, {"hazard_type": "red_zone"}])
    assert clean["safety_score"] == 1.0
    assert risky["safety_score"] < clean["safety_score"]
    assert clean["risk_level"] == "low"


def test_transit_cost_orders_by_vehicle():
    truck = service.transit_cost(100, "heavy_truck")["total_cost"]
    drone = service.transit_cost(100, "drone_delivery")["total_cost"]
    assert truck > drone


def test_transit_cost_hazard_penalty():
    base = service.transit_cost(50, "light_vehicle", 0)["total_cost"]
    penalised = service.transit_cost(50, "light_vehicle", 2)["total_cost"]
    assert penalised == base + 2 * service.HAZARD_PENALTY


def test_hazards_to_avoid_rings_point_and_polygon():
    rings = service.hazards_to_avoid_rings(
        [
            {"location": {"lat": 10, "lon": 7}, "radius_km": 1.0},
            {"geometry": {"type": "Polygon", "coordinates": [[[7, 10], [8, 10], [8, 11], [7, 10]]]}},
        ]
    )
    assert len(rings) == 2
    assert rings[1] == [[7, 10], [8, 10], [8, 11], [7, 10]]


def test_route_distance_km():
    d = service.route_distance_km([[0, 0], [0, 1]])
    assert 100 < d < 120


# --- HTTP surface --------------------------------------------------------------

@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(session_factory=factory, routing_engine=StraightLineEngine())
    app.testing = True
    return app.test_client()


def test_health(client):
    body = client.get("/health").get_json()
    assert body["service"] == "traas" and body["brand"] == "prisaTravel"


def test_safe_route(client):
    r = client.post(
        "/api/routes/safe",
        json={"origin": [10.5, 7.4], "destination": [11.8, 13.1]},
    )
    assert r.status_code == 200
    assert r.get_json()["route"]["distance_km"] > 0


def test_hazard_and_optimize_flow(client):
    client.post(
        "/api/hazards",
        json={"hazard_id": "hz1", "hazard_type": "safety", "severity": "Critical",
              "location": {"lat": 10.8, "lon": 7.6}, "radius_km": 3},
    )
    assert len(client.get("/api/hazards").get_json()["hazards"]) == 1
    r = client.post(
        "/api/routes/optimize",
        json={"origin": [10.5, 7.4], "destination": [11.8, 13.1], "vehicle_type": "heavy_truck"},
    )
    assert r.status_code == 201
    body = r.get_json()
    assert body["safety"]["safety_score"] < 1.0  # hazard reduced safety
    assert body["cost"]["vehicle_type"] == "heavy_truck"
    # route persisted
    assert len(client.get("/api/routes").get_json()["routes"]) == 1


def test_resolve_hazard(client):
    client.post(
        "/api/hazards",
        json={"hazard_id": "hz1", "hazard_type": "flood", "location": {"lat": 1, "lon": 1}},
    )
    assert client.delete("/api/hazards/hz1").status_code == 200
    assert client.get("/api/hazards").get_json()["hazards"] == []
    assert client.delete("/api/hazards/missing").status_code == 404
