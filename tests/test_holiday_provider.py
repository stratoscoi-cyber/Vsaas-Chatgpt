"""Tests for the maintained holiday-library provider integration."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.common.holiday_provider import (
    LibraryHolidayProvider, NullHolidayProvider, make_holiday_provider,
)
from agri_platform.marketplace import calendar_service
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base, Holiday

holidays_lib = pytest.importorskip("holidays")


def test_library_provider_returns_real_dates():
    from datetime import date
    provider = LibraryHolidayProvider()
    ng = provider.dates("NG", 2026)
    assert date(2026, 1, 1) in ng  # New Year's Day
    assert len(ng) > 5


def test_library_provider_unknown_region_empty():
    assert LibraryHolidayProvider().dates("ZZ", 2026) == {}


def test_make_provider_modes():
    assert isinstance(make_holiday_provider("admin"), NullHolidayProvider)
    assert isinstance(make_holiday_provider("library"), LibraryHolidayProvider)


@pytest.fixture
def factory():
    engine, f = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    return f


def test_holidays_endpoint_uses_library(factory):
    app = create_app(
        settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False,
                          holiday_provider="library"),
        session_factory=factory,
    )
    body = app.test_client().get("/api/holidays?region=NG&year=2026").get_json()
    assert body["source"] == "library"
    assert len(body["holidays"]) > 5
    assert {"date": "2026-01-01", "name": "New Year's Day"} in body["holidays"]


def test_admin_overrides_library_on_same_date(factory):
    s = factory()
    s.add(Holiday(code="NG-NY", region_code="NG", name="Admin New Year",
                  recurrence="fixed", month=1, day=1, active=True))
    s.commit()
    app = create_app(
        settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False,
                          holiday_provider="library"),
        session_factory=factory,
    )
    holidays = app.test_client().get("/api/holidays?region=NG&year=2026").get_json()["holidays"]
    jan1 = [h for h in holidays if h["date"] == "2026-01-01"]
    assert len(jan1) == 1 and jan1[0]["name"] == "Admin New Year"  # admin wins
