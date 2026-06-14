"""Tests for the WFAAS service logic and HTTP surface."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.weather import StaticWeatherClient
from agri_platform.wfaas import service
from agri_platform.wfaas.app import create_app
from agri_platform.wfaas.models import Base


# --- pure evaluators -----------------------------------------------------------

def test_alert_confidence_inverse_to_severity():
    assert service.alert_confidence("Critical") < service.alert_confidence("Low")


def test_weather_hazard_triggers_on_precip():
    spec = service.evaluate_weather_hazard([10, 85, 30], [2, 3])
    assert spec is not None and spec["severity"] == "High"


def test_weather_hazard_triggers_on_wind():
    spec = service.evaluate_weather_hazard([10, 20], [5, 25])
    assert spec is not None and "wind" in spec["message"].lower()


def test_weather_hazard_none_when_calm():
    assert service.evaluate_weather_hazard([10, 20], [1, 2]) is None


def test_soil_condition_critical_and_high():
    assert service.evaluate_soil_condition([0.05])["severity"] == "Critical"
    assert service.evaluate_soil_condition([0.15])["severity"] == "High"
    assert service.evaluate_soil_condition([0.30]) is None
    assert service.evaluate_soil_condition([]) is None


# --- HTTP surface --------------------------------------------------------------

@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    wx = StaticWeatherClient(
        hourly_values={
            "precipitation_probability": [90, 95],
            "wind_speed_10m": [3, 4],
            "soil_moisture_1_to_3cm": [0.05, 0.04],
        },
        daily_values={
            "temperature_2m_max": [20, 30],
            "temperature_2m_min": [10, 10],
        },
        times=["2026-06-14T00:00", "2026-06-14T01:00"],
    )
    app = create_app(session_factory=factory, weather_client=wx)
    app.testing = True
    return app.test_client()


def test_health(client):
    assert client.get("/health").get_json()["status"] == "healthy"


def test_register_and_get_farm(client):
    r = client.post("/api/farms", json={"farm_id": "f1", "name": "North", "owner": "Ada"})
    assert r.status_code == 201
    assert client.get("/api/farms/f1").get_json()["name"] == "North"
    # duplicate
    assert client.post("/api/farms", json={"farm_id": "f1"}).status_code == 409


def test_gdd_endpoint(client):
    r = client.post("/api/agronomy/gdd", json={"lat": 10, "lon": 7, "base_temp": 10})
    body = r.get_json()
    assert body["total"] == 15.0  # (5) + (10)


def test_drone_mission_lifecycle(client):
    client.post("/api/farms", json={"farm_id": "f1"})
    client.post("/api/drones", json={"drone_id": "d1", "capabilities": ["camera"]})
    r = client.post(
        "/api/drones/missions",
        json={"farm_id": "f1", "mission_type": "health_scan", "waypoints": [[10, 7]]},
    )
    assert r.status_code == 201
    mission_id = r.get_json()["mission_id"]
    # drone is now busy -> scheduling another fails
    r2 = client.post(
        "/api/drones/missions",
        json={"farm_id": "f1", "mission_type": "health_scan", "waypoints": [[10, 7]]},
    )
    assert r2.status_code == 409
    # complete it -> drone freed
    done = client.put(f"/api/missions/{mission_id}/result", json={"results": {"ndvi": 0.8}})
    assert done.get_json()["status"] == "completed"
    assert client.get("/api/drones").get_json()["drones"][0]["status"] == "available"


def test_detect_weather_alert_persists(client):
    client.post("/api/farms", json={"farm_id": "f1"})
    r = client.post(
        "/api/alerts/detect",
        json={"farm_id": "f1", "lat": 10, "lon": 7, "alert_type": "weather_hazard"},
    )
    assert r.status_code == 201
    alerts = client.get("/api/alerts?farm_id=f1").get_json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["alert_type"] == "weather_hazard"
