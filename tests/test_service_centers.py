"""Tests for the enterprise service-station onboarding workflow and full
tenant-isolation coverage."""

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
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
    return app.test_client()


def _config(client, docs=("accreditation", "business_license", "tax_certificate")):
    for code, t in [("ACC", "inspection"), ("REG", "licensing"), ("TAX", "licensing")]:
        client.post("/api/admin/agencies", headers=ADMIN, json={"code": code, "name": code,
                    "agency_type": t, "region_code": "NG"})
    client.post("/api/admin/region-rules", headers=ADMIN, json={"region_code": "NG",
                "required_service_center_docs": list(docs)})


def _doc(client, cid, doc_type, issuer="ACC", ref=None):
    client.post("/api/compliance/documents", json={"entity_type": "service_center", "entity_id": cid,
                "doc_type": doc_type, "issuer_code": issuer, "reference": ref or f"{cid}-{doc_type}",
                "verified": True, "expiry_on": FUTURE})


# --- workflow ------------------------------------------------------------------

def test_submit_without_rule_is_review(client):
    client.post("/api/service-centers", json={"center_id": "sc1", "region_code": "NG", "owner_id": "o"})
    out = client.post("/api/service-centers/sc1/submit").get_json()
    assert out["status"] == "under_review"  # no rule configured -> review


def test_submit_rejects_missing_documents(client):
    _config(client)
    client.post("/api/service-centers", json={"center_id": "sc2", "region_code": "NG", "owner_id": "o"})
    _doc(client, "sc2", "accreditation")  # missing business_license + tax_certificate
    out = client.post("/api/service-centers/sc2/submit").get_json()
    assert out["status"] == "rejected"
    assert any("business_license" in r for r in out["decision"]["reasons"])


def test_submit_approves_when_complete(client):
    _config(client)
    client.post("/api/service-centers", json={"center_id": "sc3", "region_code": "NG", "owner_id": "owner3",
                "business_reg_no": "RC123"})
    for dt, iss in [("accreditation", "ACC"), ("business_license", "REG"), ("tax_certificate", "TAX")]:
        _doc(client, "sc3", dt, issuer=iss)
    out = client.post("/api/service-centers/sc3/submit").get_json()
    assert out["status"] == "approved"
    assert out["risk"]["risk_band"] == "low"


def test_self_inspection_ring_raises_risk_to_review(client):
    _config(client)
    # centre owner also owns a vehicle that the centre has inspected
    client.post("/api/service-centers", json={"center_id": "scx", "region_code": "NG", "owner_id": "ownerX"})
    client.post("/api/vehicles", json={"vehicle_id": "vx", "owner_id": "ownerX", "vehicle_type": "truck_large",
                "capacity_kg": 10000, "region_code": "NG"})
    client.post("/api/vehicles/vx/inspection", json={"center_id": "scx", "result": "pass", "valid_until": FUTURE})
    for dt, iss in [("accreditation", "ACC"), ("business_license", "REG"), ("tax_certificate", "TAX")]:
        _doc(client, "scx", dt, issuer=iss)
    out = client.post("/api/service-centers/scx/submit").get_json()
    assert "self_inspection_ring" in out["risk"]["signals"]
    assert out["status"] == "under_review"  # risk high -> human review


def test_admin_status_transitions(client):
    _config(client)
    client.post("/api/service-centers", json={"center_id": "sc4", "region_code": "NG", "owner_id": "o"})
    # suspend requires admin key
    assert client.post("/api/service-centers/sc4/status", json={"action": "suspend", "decided_by": "x"}).status_code in (403, 404)
    out = client.post("/api/admin/service-centers/sc4/status", headers=ADMIN,
                      json={"action": "suspend", "reason": "fraud probe", "decided_by": "ops"}).get_json()
    assert out["center"]["compliance_status"] == "suspended"
    assert out["center"]["suspended_reason"] == "fraud probe"
    reinstated = client.post("/api/admin/service-centers/sc4/status", headers=ADMIN,
                             json={"action": "reinstate", "decided_by": "ops"}).get_json()
    assert reinstated["center"]["compliance_status"] == "approved"


def test_reputation_and_detail(client):
    client.post("/api/service-centers", json={"center_id": "sc5", "region_code": "NG", "owner_id": "o"})
    client.post("/api/vehicles", json={"vehicle_id": "v5", "owner_id": "z", "vehicle_type": "truck_large",
                "capacity_kg": 10000})
    client.post("/api/vehicles/v5/inspection", json={"center_id": "sc5", "result": "pass", "valid_until": FUTURE})
    client.post("/api/ratings", json={"subject_type": "service_center", "subject_id": "sc5", "score": 4})
    rep = client.get("/api/service-centers/sc5/reputation").get_json()
    assert rep["avg_score"] == 4.0 and rep["performance"]["inspections"] == 1
    detail = client.get("/api/service-centers/sc5").get_json()
    assert detail["performance"]["pass_rate"] == 1.0 and "risk" in detail


# --- complete tenant coverage --------------------------------------------------

@pytest.fixture
def tclient():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(settings=Settings(service="lgaas", database_url="sqlite:///:memory:",
                     log_json=False, admin_api_keys=frozenset({"k"}), tenant_isolation=True),
                     session_factory=factory)
    app.testing = True
    return app.test_client()


def test_vehicles_scoped_by_tenant(tclient):
    tclient.post("/api/vehicles", headers={"X-Tenant-ID": "tA"},
                 json={"vehicle_id": "va", "owner_id": "o", "vehicle_type": "truck_large", "capacity_kg": 100})
    tclient.post("/api/vehicles", headers={"X-Tenant-ID": "tB"},
                 json={"vehicle_id": "vb", "owner_id": "o", "vehicle_type": "truck_large", "capacity_kg": 100})
    listed = tclient.get("/api/vehicles", headers={"X-Tenant-ID": "tA"}).get_json()["vehicles"]
    assert [v["vehicle_id"] for v in listed] == ["va"]


def test_service_centers_scoped_by_tenant(tclient):
    tclient.post("/api/service-centers", headers={"X-Tenant-ID": "tA"},
                 json={"center_id": "ca", "region_code": "NG", "owner_id": "o"})
    tclient.post("/api/service-centers", headers={"X-Tenant-ID": "tB"},
                 json={"center_id": "cb", "region_code": "NG", "owner_id": "o"})
    listed = tclient.get("/api/service-centers", headers={"X-Tenant-ID": "tB"}).get_json()["service_centers"]
    assert [c["center_id"] for c in listed] == ["cb"]
    # cross-tenant detail blocked
    assert tclient.get("/api/service-centers/ca", headers={"X-Tenant-ID": "tB"}).status_code == 404


def test_escrow_scoped_by_tenant(tclient):
    # tenant A creates load + vehicle + offer + award -> escrow stamped tA
    tclient.post("/api/vehicles", headers={"X-Tenant-ID": "tA"},
                 json={"vehicle_id": "v1", "owner_id": "o", "vehicle_type": "truck_large", "capacity_kg": 20000})
    load = tclient.post("/api/loads", headers={"X-Tenant-ID": "tA"}, json={"shipper_id": "s", "weight_kg": 8000,
                        "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    offer = tclient.post(f"/api/loads/{load['ref']}/offers", headers={"X-Tenant-ID": "tA"},
                         json={"bidder_id": "c", "bidder_role": "transporter", "price": 1000, "vehicle_id": "v1"}).get_json()
    acc = tclient.post(f"/api/offers/{offer['id']}/accept", headers={"X-Tenant-ID": "tA"}, json={}).get_json()
    eid = acc["escrow"]["escrow_id"]
    assert acc["escrow"]["tenant_id"] == "tA"
    # tenant B cannot read tenant A's escrow
    assert tclient.get(f"/api/escrows/{eid}", headers={"X-Tenant-ID": "tB"}).status_code == 404
    assert tclient.get(f"/api/escrows/{eid}", headers={"X-Tenant-ID": "tA"}).status_code == 200


def test_compliance_decisions_scoped(tclient):
    # decisions inherit tenant via the central before-flush stamp
    tclient.post("/api/drivers", headers={"X-Tenant-ID": "tA"}, json={"driver_id": "dA", "region_code": "NG"})
    tclient.post("/api/drivers/dA/submit", headers={"X-Tenant-ID": "tA"})
    tclient.post("/api/drivers", headers={"X-Tenant-ID": "tB"}, json={"driver_id": "dB", "region_code": "NG"})
    tclient.post("/api/drivers/dB/submit", headers={"X-Tenant-ID": "tB"})
    decisions = tclient.get("/api/compliance/decisions", headers={"X-Tenant-ID": "tA"}).get_json()["decisions"]
    assert decisions and all(d["entity_id"] == "dA" for d in decisions)
