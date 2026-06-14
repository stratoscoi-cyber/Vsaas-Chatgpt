"""Auth, rate limiting and error-envelope behaviour, exercised via the WFAAS app."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.common.weather import StaticWeatherClient
from agri_platform.wfaas.app import create_app
from agri_platform.wfaas.models import Base


def _factory():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    return factory


def _settings(**over):
    base = Settings(service="wfaas", database_url="sqlite:///:memory:", log_json=False)
    return base.with_overrides(**over)


def test_auth_required_when_enabled():
    app = create_app(
        settings=_settings(auth_enabled=True, api_keys=frozenset({"secret"})),
        session_factory=_factory(),
        weather_client=StaticWeatherClient(),
    )
    client = app.test_client()
    # health is exempt
    assert client.get("/health").status_code == 200
    # protected without key
    assert client.get("/api/farms").status_code == 401
    # protected with wrong key
    assert client.get("/api/farms", headers={"X-API-Key": "nope"}).status_code == 401
    # protected with right key
    assert client.get("/api/farms", headers={"X-API-Key": "secret"}).status_code == 200
    # Bearer form also works
    assert client.get("/api/farms", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_rate_limiting():
    app = create_app(
        settings=_settings(rate_limit_per_minute=2),
        session_factory=_factory(),
        weather_client=StaticWeatherClient(),
    )
    client = app.test_client()
    assert client.get("/api/drones").status_code == 200
    assert client.get("/api/drones").status_code == 200
    r = client.get("/api/drones")
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in r.headers


def test_error_envelope_validation():
    app = create_app(settings=_settings(), session_factory=_factory(), weather_client=StaticWeatherClient())
    client = app.test_client()
    r = client.post("/api/farms", json={})  # missing farm_id
    assert r.status_code == 422
    body = r.get_json()
    assert body["error"]["code"] == "validation_error"
    assert "farm_id" in body["error"]["details"]["missing"]


def test_request_id_header():
    app = create_app(settings=_settings(), session_factory=_factory(), weather_client=StaticWeatherClient())
    r = app.test_client().get("/health")
    assert r.headers.get("X-Request-ID")


def test_readyz():
    app = create_app(settings=_settings(), session_factory=_factory(), weather_client=StaticWeatherClient())
    assert app.test_client().get("/readyz").get_json()["status"] == "ready"
