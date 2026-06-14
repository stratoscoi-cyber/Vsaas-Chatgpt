"""Tests for the unified party onboarding engine (operators, fleet managers,
inspection agents, MSPs) and the driver lifecycle."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.marketplace import kyc
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base

ADMIN = {"X-Admin-Key": "k"}
FUTURE = "2027-01-01"


@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(settings=Settings(service="lgaas", database_url="sqlite:///:memory:",
                     log_json=False, admin_api_keys=frozenset({"k"})), session_factory=factory)
    app.testing = True
    return app, app.test_client()


def _agency(client, code="LIC", atype="licensing"):
    client.post("/api/admin/agencies", headers=ADMIN, json={"code": code, "name": code,
                "agency_type": atype, "region_code": "NG"})


def _role_req(client, role, **over):
    payload = {"region_code": "NG", "role": role, "required_docs": ["business_license"]}
    payload.update(over)
    return client.post("/api/admin/role-requirements", headers=ADMIN, json=payload)


def _doc(client, role, pid, doc_type="business_license", issuer="LIC"):
    client.post("/api/compliance/documents", json={"entity_type": role, "entity_id": pid,
                "doc_type": doc_type, "issuer_code": issuer, "reference": f"{pid}{doc_type}",
                "verified": True, "expiry_on": FUTURE})


# --- admin config gating -------------------------------------------------------

def test_role_requirements_admin_only(client):
    _, c = client
    assert c.post("/api/admin/role-requirements", json={}).status_code == 403


def test_invalid_role_rejected(client):
    _, c = client
    assert _role_req(c, "wizard").status_code == 422
    assert c.post("/api/parties", json={"party_id": "p", "party_type": "wizard"}).status_code == 422


# --- unified workflow ----------------------------------------------------------

def test_no_requirement_is_review(client):
    _, c = client
    c.post("/api/parties", json={"party_id": "op1", "party_type": "logistics_operator", "region_code": "NG"})
    assert c.post("/api/parties/op1/submit").get_json()["status"] == "under_review"


def test_operator_rejected_without_docs(client):
    _, c = client
    _agency(c)
    _role_req(c, "logistics_operator", required_docs=["business_license", "tax_certificate"])
    c.post("/api/parties", json={"party_id": "op2", "party_type": "logistics_operator", "region_code": "NG"})
    _doc(c, "logistics_operator", "op2", "business_license")  # missing tax_certificate
    out = c.post("/api/parties/op2/submit").get_json()
    assert out["status"] == "rejected"
    assert any("tax_certificate" in r for r in out["decision"]["reasons"])


def test_fleet_manager_approved_when_complete(client):
    _, c = client
    _agency(c)
    _role_req(c, "fleet_manager", required_docs=["business_license"], min_experience_years=3)
    c.post("/api/parties", json={"party_id": "fm1", "party_type": "fleet_manager", "region_code": "NG",
                                 "experience_years": 5, "name": "Ada"})
    _doc(c, "fleet_manager", "fm1")
    out = c.post("/api/parties/fm1/submit").get_json()
    assert out["status"] == "approved" and out["risk"]["risk_band"] == "low"


def test_fleet_manager_rejected_low_experience(client):
    _, c = client
    _agency(c)
    _role_req(c, "fleet_manager", required_docs=["business_license"], min_experience_years=5)
    c.post("/api/parties", json={"party_id": "fm2", "party_type": "fleet_manager", "region_code": "NG",
                                 "experience_years": 1})
    _doc(c, "fleet_manager", "fm2")
    out = c.post("/api/parties/fm2/submit").get_json()
    assert out["status"] == "rejected" and any("experience" in r for r in out["decision"]["reasons"])


def test_inspection_agent_conflict_of_interest(client):
    app, c = client
    _agency(c)
    _role_req(c, "inspection_agent", required_docs=["business_license"])
    # agent owner also owns a vehicle -> not independent -> review
    c.post("/api/vehicles", json={"vehicle_id": "v1", "owner_id": "agentco", "vehicle_type": "truck_large",
                                  "capacity_kg": 100})
    c.post("/api/parties", json={"party_id": "ia1", "party_type": "inspection_agent", "region_code": "NG",
                                 "owner_id": "agentco"})
    _doc(c, "inspection_agent", "ia1")
    out = c.post("/api/parties/ia1/submit").get_json()
    assert "conflict_of_interest" in out["risk"]["signals"]
    assert out["status"] == "under_review"


def test_msp_screening_required_but_unscreened_is_review(client):
    app, c = client
    _agency(c)
    _role_req(c, "msp", required_docs=["business_license"], required_screening=True)
    c.post("/api/parties", json={"party_id": "msp1", "party_type": "msp", "region_code": "NG", "name": "Globex"})
    _doc(c, "msp", "msp1")
    # default manual provider does not screen -> cannot confirm clearance -> review
    out = c.post("/api/parties/msp1/submit").get_json()
    assert out["screening_status"] == "not_screened"
    assert out["status"] == "under_review"


def test_msp_screening_hit_rejects(client):
    app, c = client

    class _Screener:
        name = "stub"
        def verify_document(self, d): return kyc.VerificationResult(True, "verified", {})
        def screen(self, identity): return kyc.ScreeningResult(False, "screened", [{"list": "sanctions"}])

    app.config["KYC_PROVIDER"] = _Screener()
    _agency(c)
    _role_req(c, "msp", required_docs=["business_license"], required_screening=True)
    c.post("/api/parties", json={"party_id": "msp2", "party_type": "msp", "region_code": "NG", "name": "BAD"})
    _doc(c, "msp", "msp2")
    out = c.post("/api/parties/msp2/submit").get_json()
    assert out["status"] == "rejected" and "screening_hit" in out["risk"]["signals"]


def test_admin_transitions_party(client):
    _, c = client
    c.post("/api/parties", json={"party_id": "op9", "party_type": "logistics_operator", "region_code": "NG"})
    out = c.post("/api/admin/parties/op9/status", headers=ADMIN,
                 json={"action": "suspend", "reason": "audit", "decided_by": "ops"}).get_json()
    assert out["status"] == "suspended"
    assert c.get("/api/parties/op9").get_json()["party"]["compliance_status"] == "suspended"


# --- driver lifecycle ----------------------------------------------------------

def test_driver_lifecycle_status_and_transition(client):
    _, c = client
    _agency(c, code="DVLA")
    c.post("/api/admin/region-rules", headers=ADMIN, json={"region_code": "NG",
           "required_driver_docs": ["drivers_license"], "min_experience_years": 2})
    c.post("/api/drivers", json={"driver_id": "d1", "region_code": "NG", "experience_years": 4, "name": "Musa"})
    c.post("/api/compliance/documents", json={"entity_type": "driver", "entity_id": "d1",
           "doc_type": "drivers_license", "issuer_code": "DVLA", "reference": "DL1", "verified": True,
           "expiry_on": FUTURE})
    out = c.post("/api/drivers/d1/submit").get_json()
    assert out["status"] == "approved" and out["risk"]["risk_band"] == "low"
    # suspend then reinstate
    susp = c.post("/api/admin/drivers/d1/status", headers=ADMIN,
                  json={"action": "suspend", "reason": "complaint", "decided_by": "ops"}).get_json()
    assert susp["status"] == "suspended"
    assert c.get("/api/drivers/d1").get_json()["party"]["compliance_status"] == "suspended"
