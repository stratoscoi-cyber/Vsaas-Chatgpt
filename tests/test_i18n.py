"""Tests for Africa-centric regions, i18n and localized services."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common import i18n, regions
from agri_platform.common.config import Settings
from agri_platform.common.weather import StaticWeatherClient
from agri_platform.marketplace.app import create_app as create_lgaas
from agri_platform.marketplace.models import Base as LBase
from agri_platform.traas.app import create_app as create_traas
from agri_platform.traas.models import Base as TBase
from agri_platform.wfaas.app import create_app as create_wfaas
from agri_platform.wfaas.models import Base as WBase


# --- regions -------------------------------------------------------------------

def test_region_for_point_nigeria():
    r = regions.region_for_point(10.5, 7.4)
    assert r.code == "NG" and r.currency == "NGN"


def test_region_for_point_kenya_is_swahili():
    r = regions.region_for_point(-1.29, 36.82)  # Nairobi
    assert r.code == "KE" and r.primary_language == "sw"


def test_region_for_point_outside_africa_defaults():
    r = regions.region_for_point(48.85, 2.35)  # Paris
    assert r.code == regions.DEFAULT_REGION.code


def test_region_for_coords_none_without_coords():
    assert regions.region_for_coords(None) is None
    assert regions.region_for_coords({"lat": None, "lon": None}) is None


# --- i18n ----------------------------------------------------------------------

def test_translate_french():
    assert i18n.translate("greeting.default", "fr") == "Bonjour,"


def test_translate_fallback_to_english():
    # Hausa catalog has greetings only -> closing falls back to English.
    assert i18n.translate("closing.default", "ha") == i18n.CATALOG["en"]["closing.default"]


def test_translate_param_substitution():
    msg = i18n.translate("offer.direct", "pt", price="USD 100")
    assert "USD 100" in msg


def test_resolve_language_priority():
    assert i18n.resolve_language(explicit="fr") == "fr"
    assert i18n.resolve_language(accept_language="de,sw;q=0.8") == "sw"
    assert i18n.resolve_language(region_language="pt") == "pt"
    assert i18n.resolve_language(explicit="xx", default="en") == "en"


# --- localization endpoints (present on every service) -------------------------

@pytest.mark.parametrize("factory,base,extra", [
    (create_wfaas, WBase, {"weather_client": StaticWeatherClient()}),
    (create_traas, TBase, {}),
    (create_lgaas, LBase, {}),
])
def test_localization_endpoints_on_all_services(factory, base, extra):
    engine, sf = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, base)
    app = factory(settings=Settings(service="x", database_url="sqlite:///:memory:", log_json=False),
                  session_factory=sf, **extra)
    c = app.test_client()
    langs = c.get("/api/i18n/languages").get_json()["languages"]
    assert {l["code"] for l in langs} >= {"en", "fr", "sw", "ar"}
    regs = c.get("/api/regions").get_json()["regions"]
    assert any(r["code"] == "NG" for r in regs)
    lk = c.get("/api/regions/lookup?lat=-1.29&lon=36.82").get_json()
    assert lk["region"]["code"] == "KE"
    # Content-Language reflects the negotiated locale
    assert c.get("/health", headers={"Accept-Language": "fr"}).headers.get("Content-Language") == "fr"


# --- marketplace localized behaviour ------------------------------------------

@pytest.fixture
def lgaas():
    engine, sf = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, LBase)
    app = create_lgaas(settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False),
                       session_factory=sf)
    app.testing = True
    return app.test_client()


def test_currency_auto_from_region(lgaas):
    # Senegal origin -> XOF
    load = lgaas.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 1000,
        "origin": {"lat": 14.7, "lon": -17.4}, "destination": {"lat": 14.0, "lon": -16.0},
    }).get_json()
    assert load["currency"] == "XOF"
    assert load["region"]["code"] == "SN"


def test_currency_explicit_overrides_region(lgaas):
    load = lgaas.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 1000, "currency": "USD",
        "origin": {"lat": 10.5, "lon": 7.4},
    }).get_json()
    assert load["currency"] == "USD"


def test_haggle_localized_to_french(lgaas):
    load = lgaas.post("/api/loads", json={
        "shipper_id": "s1", "title": "Mangues", "weight_kg": 5000,
        "origin": {"lat": 14.7, "lon": -17.4}, "destination": {"lat": 14.0, "lon": -16.0},
        "budget": 500,
    }).get_json()
    offer = lgaas.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "t1", "bidder_role": "transporter", "price": 900,
        "negotiation_style": "relationship_first",
    }).get_json()
    res = lgaas.post(f"/api/offers/{offer['id']}/haggle", json={
        "as_role": "shipper", "lang": "fr", "counterparty_name": "M. Diop",
    }).get_json()
    assert res["language"] == "fr"
    assert "Bonjour" in res["message"]
    assert res["fair_rate"]["currency"] == "XOF"


def test_haggle_auto_language_from_region(lgaas):
    # Kenya origin -> Swahili by default (no explicit lang)
    load = lgaas.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 5000,
        "origin": {"lat": -1.29, "lon": 36.82}, "destination": {"lat": -1.0, "lon": 37.0},
        "budget": 500,
    }).get_json()
    offer = lgaas.post(f"/api/loads/{load['ref']}/offers", json={
        "bidder_id": "t1", "bidder_role": "transporter", "price": 900}).get_json()
    res = lgaas.post(f"/api/offers/{offer['id']}/haggle", json={"as_role": "shipper"}).get_json()
    assert res["language"] == "sw"
    assert "Habari" in res["message"]
