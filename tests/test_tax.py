"""Tests for the tax engine, admin tax-rule API and quote integration."""

from datetime import date

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common import tax
from agri_platform.common.config import Settings
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base

ADMIN = {"X-Admin-Key": "admin-secret"}


# --- pure engine ---------------------------------------------------------------

def test_no_rules_means_zero_and_unconfigured():
    r = tax.compute_taxes(1000, [])
    assert r["total_add"] == 0 and r["total_withheld"] == 0
    assert r["gross_total"] == 1000.0
    assert r["configured"] is False and r["rules_applied"] == 0


def test_vat_add_on_top():
    rules = [{"code": "VAT", "name": "VAT", "tax_type": "vat", "collection": "add", "rate_percent": 7.5}]
    r = tax.compute_taxes(1000, rules)
    assert r["total_add"] == 75.0
    assert r["gross_total"] == 1075.0
    assert r["net_payable_to_provider"] == 1000.0


def test_withholding_reduces_payout_not_total():
    rules = [{"code": "WHT", "collection": "withhold", "rate_percent": 5}]
    r = tax.compute_taxes(1000, rules)
    assert r["total_withheld"] == 50.0
    assert r["gross_total"] == 1000.0
    assert r["net_payable_to_provider"] == 950.0


def test_compound_is_tax_on_tax():
    rules = [
        {"code": "A", "collection": "add", "rate_percent": 10, "sequence": 1},
        {"code": "B", "collection": "add", "rate_percent": 10, "sequence": 2, "basis": "compound"},
    ]
    r = tax.compute_taxes(100, rules)
    # A: 10 on 100 = 10 ; B compound: 10% of (100+10) = 11
    amounts = {l["code"]: l["amount"] for l in r["lines"]}
    assert amounts["A"] == 10.0 and amounts["B"] == 11.0
    assert r["gross_total"] == 121.0


def test_threshold_skips_small_base():
    rules = [{"code": "LEVY", "collection": "add", "rate_percent": 2, "threshold_min": 5000}]
    assert tax.compute_taxes(1000, rules)["rules_applied"] == 0
    assert tax.compute_taxes(6000, rules)["rules_applied"] == 1


def test_effective_window():
    rules = [{"code": "VAT", "collection": "add", "rate_percent": 10,
              "effective_from": "2026-01-01", "effective_to": "2026-12-31"}]
    assert tax.compute_taxes(100, rules, on_date=date(2025, 6, 1))["rules_applied"] == 0
    assert tax.compute_taxes(100, rules, on_date=date(2026, 6, 1))["rules_applied"] == 1


def test_category_filter():
    rules = [{"code": "VAT", "collection": "add", "rate_percent": 10, "applies_to": ["goods"]}]
    assert tax.compute_taxes(100, rules, category="transport_service")["rules_applied"] == 0
    assert tax.compute_taxes(100, rules, category="goods")["rules_applied"] == 1


def test_rounding_half_up():
    rules = [{"code": "VAT", "collection": "add", "rate_percent": 7.5}]
    # 333.33 * 7.5% = 24.99975 -> 25.00
    r = tax.compute_taxes(333.33, rules)
    assert r["total_add"] == 25.0


# --- admin API + integration ---------------------------------------------------

@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(
        settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False,
                          admin_api_keys=frozenset({"admin-secret"})),
        session_factory=factory,
    )
    app.testing = True
    return app.test_client()


def test_admin_requires_key(client):
    # no key
    assert client.get("/api/admin/tax-rules").status_code == 403
    # wrong key
    assert client.get("/api/admin/tax-rules", headers={"X-Admin-Key": "nope"}).status_code == 403
    # right key
    assert client.get("/api/admin/tax-rules", headers=ADMIN).status_code == 200


def test_admin_disabled_without_configured_keys():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False),
                     session_factory=factory)
    r = app.test_client().get("/api/admin/tax-rules")
    assert r.status_code == 403 and r.get_json()["error"]["code"] == "admin_disabled"


def test_tax_rule_crud(client):
    create = client.post("/api/admin/tax-rules", headers=ADMIN, json={
        "code": "NG-VAT", "region_code": "NG", "name": "Nigeria VAT", "tax_type": "vat",
        "collection": "add", "rate_percent": 7.5, "statutory_reference": "operator-supplied",
    })
    assert create.status_code == 201
    assert client.get("/api/admin/tax-rules/NG-VAT", headers=ADMIN).get_json()["rate_percent"] == 7.5
    # update
    up = client.put("/api/admin/tax-rules/NG-VAT", headers=ADMIN, json={"rate_percent": 10})
    assert up.get_json()["rate_percent"] == 10.0
    # validation
    bad = client.post("/api/admin/tax-rules", headers=ADMIN, json={
        "code": "X", "region_code": "NG", "collection": "bogus", "rate_percent": 1})
    assert bad.status_code == 422
    # soft delete
    client.delete("/api/admin/tax-rules/NG-VAT", headers=ADMIN)
    assert client.get("/api/admin/tax-rules/NG-VAT", headers=ADMIN).get_json()["active"] is False


def test_estimate_includes_configured_tax(client):
    client.post("/api/admin/tax-rules", headers=ADMIN, json={
        "code": "NG-VAT", "region_code": "NG", "collection": "add", "rate_percent": 7.5,
        "applies_to": ["transport_service"]})
    load = client.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 10000, "load_type": "grain",
        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    est = client.post(f"/api/loads/{load['ref']}/estimate", json={}).get_json()
    assert est["tax"]["configured"] is True
    assert est["tax"]["rules_applied"] == 1
    assert est["tax"]["region_code"] == "NG"
    # gross = base + VAT
    base = est["estimate"]["recommended"]
    assert round(est["tax"]["gross_total"], 2) == round(base * 1.075, 2)


def test_estimate_without_rules_reports_unconfigured(client):
    load = client.post("/api/loads", json={
        "shipper_id": "s1", "weight_kg": 10000,
        "origin": {"lat": 10.5, "lon": 7.4}}).get_json()
    est = client.post(f"/api/loads/{load['ref']}/estimate", json={}).get_json()
    assert est["tax"]["configured"] is False
    assert est["tax"]["total_add"] == 0


def test_tax_quote_endpoint(client):
    client.post("/api/admin/tax-rules", headers=ADMIN, json={
        "code": "ALL-LEVY", "region_code": "*", "collection": "add", "rate_percent": 1})
    r = client.post("/api/tax/quote", json={"base_amount": 1000, "region_code": "GH", "currency": "GHS"})
    body = r.get_json()
    # global "*" rule applies to any region
    assert body["total_add"] == 10.0 and body["currency"] == "GHS"
