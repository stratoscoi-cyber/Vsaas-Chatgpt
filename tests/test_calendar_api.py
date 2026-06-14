"""Tests for LGaaS calendar/holiday endpoints, region profile and currency API."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base

ADMIN = {"X-Admin-Key": "k"}


@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(
        settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False,
                          admin_api_keys=frozenset({"k"})),
        session_factory=factory,
    )
    app.testing = True
    return app.test_client()


def test_currencies_endpoint(client):
    codes = {c["code"] for c in client.get("/api/i18n/currencies").get_json()["currencies"]}
    assert {"NGN", "XOF", "KES"} <= codes


def test_format_endpoint_localized(client):
    r = client.get("/api/i18n/format?amount=1234.5&currency=NGN", headers={"Accept-Language": "fr"})
    body = r.get_json()
    assert body["locale"] == "fr" and body["formatted"] == "₦1 234,50"


def test_region_profile(client):
    body = client.get("/api/regions/NG/profile").get_json()
    assert body["weekend"] == [5, 6]
    assert body["default_negotiation_style"] == "market_bargaining"
    assert body["currency"]["code"] == "NGN"
    assert "negotiation_profile" in body


def test_holiday_admin_requires_key(client):
    assert client.post("/api/admin/holidays", json={}).status_code == 403


def test_holiday_lifecycle_and_calendar(client):
    # Admin configures a real, operator-entered holiday.
    r = client.post("/api/admin/holidays", headers=ADMIN, json={
        "code": "NG-NEWYEAR", "region_code": "NG", "name": "New Year's Day",
        "recurrence": "fixed", "month": 1, "day": 1})
    assert r.status_code == 201

    holidays = client.get("/api/holidays?region=NG&year=2026").get_json()["holidays"]
    assert {"date": "2026-01-01", "name": "New Year's Day"} in holidays

    # 2026-01-01 is a Thursday holiday -> not a business day
    info = client.get("/api/calendar/business-day?region=NG&date=2026-01-01").get_json()
    assert info["is_holiday"] is True
    assert info["is_business_day"] is False
    assert info["next_business_day"] == "2026-01-02"

    # add business days
    info2 = client.get("/api/calendar/business-day?region=NG&date=2026-01-02&add=5").get_json()
    assert info2["plus_business_days"]["date"] == "2026-01-09"


def test_holidays_unconfigured_is_empty(client):
    body = client.get("/api/holidays?region=GH&year=2026").get_json()
    assert body["holidays"] == []  # nothing fabricated


def test_load_schedule_flags_holiday(client):
    client.post("/api/admin/holidays", headers=ADMIN, json={
        "code": "NG-NEWYEAR", "region_code": "NG", "name": "New Year's Day",
        "recurrence": "fixed", "month": 1, "day": 1})
    load = client.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 1000,
        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1},
        "delivery_deadline": "2026-01-01"}).get_json()
    assert load["schedule"]["is_holiday"] is True
    assert load["schedule"]["next_business_day"] == "2026-01-02"


def test_estimate_currency_formatted(client):
    load = client.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 10000,
        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    est = client.post(f"/api/loads/{load['ref']}/estimate", json={}).get_json()
    assert load["currency"] == "NGN"
    assert est["estimate"]["recommended_formatted"].startswith("₦")
    assert est["tax"]["formatted"]["gross_total"].startswith("₦")
