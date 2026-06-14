"""Tests for the drone-imagery inference integration."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.common.weather import StaticWeatherClient
from agri_platform.wfaas.ai import InferenceError, UnavailableInferenceClient, make_inference_client
from agri_platform.wfaas.app import create_app
from agri_platform.wfaas.models import Base


class _FakeInference:
    def analyze(self, mission_id, image_refs, context=None):
        return {"plant_health_index": 0.91, "images_analyzed": len(image_refs)}


def test_make_inference_client_unavailable_without_endpoint():
    client = make_inference_client(None)
    assert isinstance(client, UnavailableInferenceClient)
    with pytest.raises(InferenceError):
        client.analyze("m1", [])


def _app(inference):
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    return create_app(
        settings=Settings(service="wfaas", database_url="sqlite:///:memory:", log_json=False),
        session_factory=factory,
        weather_client=StaticWeatherClient(),
        inference_client=inference,
    )


def _seed_mission(client):
    client.post("/api/farms", json={"farm_id": "f1"})
    client.post("/api/drones", json={"drone_id": "d1"})
    r = client.post(
        "/api/drones/missions",
        json={"farm_id": "f1", "mission_type": "health_scan", "waypoints": [[10, 7]]},
    )
    return r.get_json()["mission_id"]


def test_analyze_endpoint_unavailable_returns_503():
    app = _app(UnavailableInferenceClient())
    client = app.test_client()
    mission_id = _seed_mission(client)
    r = client.post(f"/api/missions/{mission_id}/analyze", json={"images": ["s3://x.jpg"]})
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "inference_unavailable"


def test_analyze_endpoint_completes_mission():
    app = _app(_FakeInference())
    client = app.test_client()
    mission_id = _seed_mission(client)
    r = client.post(f"/api/missions/{mission_id}/analyze", json={"images": ["s3://a.jpg", "s3://b.jpg"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "completed"
    assert body["results"]["images_analyzed"] == 2
    # drone freed again
    assert client.get("/api/drones").get_json()["drones"][0]["status"] == "available"


def test_analyze_missing_mission_404():
    app = _app(_FakeInference())
    r = app.test_client().post("/api/missions/nope/analyze", json={"images": []})
    assert r.status_code == 404
