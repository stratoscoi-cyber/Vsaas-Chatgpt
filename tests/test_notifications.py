"""Tests for the WFAAS notification engine and background monitoring."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.common.weather import StaticWeatherClient
from agri_platform.wfaas import service
from agri_platform.wfaas.app import create_app
from agri_platform.wfaas.models import AlertPreference, Base, Farm
from agri_platform.wfaas.monitoring import scan_farms
from agri_platform.wfaas.notifications import ConsoleNotifier, dispatch_alert


@pytest.fixture
def session():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    return factory()


_SPEC = {
    "alert_type": "weather_hazard",
    "severity": "High",
    "message": "test",
    "related_data": {},
}


def test_dispatch_requires_owner_email(session):
    session.add(Farm(farm_id="f1", name="No email"))
    session.commit()
    alert = service.create_alert(session, "f1", _SPEC, {"lat": 1, "lon": 1})
    notifier = ConsoleNotifier()
    assert dispatch_alert(session, notifier, alert) is False
    assert notifier.sent == []


def test_dispatch_sends_when_configured(session):
    session.add(Farm(farm_id="f1", owner_email="owner@example.com"))
    session.commit()
    alert = service.create_alert(session, "f1", _SPEC, {"lat": 1, "lon": 1})
    notifier = ConsoleNotifier()
    assert dispatch_alert(session, notifier, alert) is True
    assert notifier.sent[0]["to"] == "owner@example.com"


def test_dispatch_respects_preferences(session):
    session.add(Farm(farm_id="f1", owner_email="owner@example.com"))
    session.add(AlertPreference(farm_id="f1", email_enabled=False))
    session.commit()
    alert = service.create_alert(session, "f1", _SPEC, {"lat": 1, "lon": 1})
    notifier = ConsoleNotifier()
    assert dispatch_alert(session, notifier, alert) is False


def test_dispatch_respects_type_optout(session):
    session.add(Farm(farm_id="f1", owner_email="owner@example.com"))
    session.add(AlertPreference(farm_id="f1", email_enabled=True, alert_types=["soil_condition"]))
    session.commit()
    alert = service.create_alert(session, "f1", _SPEC, {"lat": 1, "lon": 1})  # weather_hazard
    assert dispatch_alert(session, ConsoleNotifier(), alert) is False


def test_scan_farms_creates_alerts_and_notifies(session):
    session.add(Farm(farm_id="f1", coordinates={"lat": 10, "lon": 7}, owner_email="o@x.com"))
    session.add(Farm(farm_id="f2"))  # no coordinates -> skipped
    session.commit()
    wx = StaticWeatherClient(
        hourly_values={
            "precipitation_probability": [95],
            "wind_speed_10m": [1],
            "soil_moisture_1_to_3cm": [0.05],
        },
        times=["2026-06-14T00:00"],
    )
    notifier = ConsoleNotifier()
    alerts = scan_farms(session, wx, notifier, today="2026-06-14")
    # precipitation + soil moisture both trigger for f1
    assert len(alerts) == 2
    assert all(a.farm_id == "f1" for a in alerts)
    assert len(notifier.sent) == 2


def test_monitoring_scan_endpoint():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    s = factory()
    s.add(Farm(farm_id="f1", coordinates={"lat": 10, "lon": 7}))
    s.commit()
    wx = StaticWeatherClient(
        hourly_values={"precipitation_probability": [99], "wind_speed_10m": [0], "soil_moisture_1_to_3cm": [0.5]},
        times=["2026-06-14T00:00"],
    )
    app = create_app(
        settings=Settings(service="wfaas", database_url="sqlite:///:memory:", log_json=False),
        session_factory=factory,
        weather_client=wx,
    )
    r = app.test_client().post("/api/monitoring/scan")
    assert r.status_code == 200
    assert r.get_json()["alerts_created"] == 1
