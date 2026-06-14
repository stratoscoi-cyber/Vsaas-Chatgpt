"""Tests for the ops console: unified review queue, dashboard, audit, transitions."""

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


def _seed_review_items(client):
    """Create an inspection-agent (conflict -> review) and an MSP (unscreened -> review)."""
    client.post("/api/admin/agencies", headers=ADMIN, json={"code": "LIC", "name": "LIC",
                "agency_type": "licensing", "region_code": "NG"})
    for role, screen in [("inspection_agent", False), ("msp", True)]:
        client.post("/api/admin/role-requirements", headers=ADMIN, json={"region_code": "NG", "role": role,
                    "required_docs": ["business_license"], "required_screening": screen})

    client.post("/api/vehicles", json={"vehicle_id": "v1", "owner_id": "agentco",
                "vehicle_type": "truck_large", "capacity_kg": 100})
    client.post("/api/parties", json={"party_id": "ia1", "party_type": "inspection_agent",
                "region_code": "NG", "owner_id": "agentco", "name": "Indie Inspect"})
    client.post("/api/compliance/documents", json={"entity_type": "inspection_agent", "entity_id": "ia1",
                "doc_type": "business_license", "issuer_code": "LIC", "reference": "BL1",
                "verified": True, "expiry_on": FUTURE})
    client.post("/api/parties/ia1/submit")

    client.post("/api/parties", json={"party_id": "msp1", "party_type": "msp", "region_code": "NG", "name": "Globex"})
    client.post("/api/compliance/documents", json={"entity_type": "msp", "entity_id": "msp1",
                "doc_type": "business_license", "issuer_code": "LIC", "reference": "BL2",
                "verified": True, "expiry_on": FUTURE})
    client.post("/api/parties/msp1/submit")


def test_ops_requires_admin(client):
    assert client.get("/api/admin/ops/dashboard").status_code == 403


def test_review_queue_aggregates_all_types(client):
    _seed_review_items(client)
    queue = client.get("/api/admin/ops/review-queue", headers=ADMIN).get_json()["queue"]
    ids = {(d["entity_type"], d["entity_id"]) for d in queue}
    assert ("inspection_agent", "ia1") in ids and ("msp", "msp1") in ids
    # each row carries an entity snapshot
    assert all("entity" in d and "risk_band" in d["entity"] for d in queue)


def test_review_queue_filter(client):
    _seed_review_items(client)
    only_msp = client.get("/api/admin/ops/review-queue?entity_type=msp", headers=ADMIN).get_json()["queue"]
    assert only_msp and all(d["entity_type"] == "msp" for d in only_msp)


def test_dashboard_counts(client):
    _seed_review_items(client)
    dash = client.get("/api/admin/ops/dashboard", headers=ADMIN).get_json()
    assert dash["review_count"] >= 2
    assert dash["by_status"].get("under_review", 0) >= 2
    assert "inspection_agent" in dash["by_entity_type"]


def test_entity_profile_and_history(client):
    _seed_review_items(client)
    prof = client.get("/api/admin/ops/entities/msp/msp1", headers=ADMIN).get_json()
    assert prof["entity"]["party_id"] == "msp1"
    assert prof["decisions"]  # has at least the submit decision
    assert client.get("/api/admin/ops/entities/msp/nope", headers=ADMIN).status_code == 404


def test_unified_transition_clears_queue(client):
    _seed_review_items(client)
    # approve the MSP from the console
    out = client.post("/api/admin/ops/entities/msp/msp1/status", headers=ADMIN,
                      json={"action": "approve", "decided_by": "ops"}).get_json()
    assert out["status"] == "approved"
    # it leaves the review queue; the entity reflects the new status
    queue_ids = {d["entity_id"] for d in client.get("/api/admin/ops/review-queue", headers=ADMIN).get_json()["queue"]}
    assert "msp1" not in queue_ids
    assert client.get("/api/parties/msp1").get_json()["party"]["compliance_status"] == "approved"


def test_transition_suspend_sets_reason(client):
    _seed_review_items(client)
    client.post("/api/admin/ops/entities/inspection_agent/ia1/status", headers=ADMIN,
                json={"action": "suspend", "reason": "conflict probe", "decided_by": "ops"})
    party = client.get("/api/parties/ia1").get_json()["party"]
    assert party["compliance_status"] == "suspended" and party["suspended_reason"] == "conflict probe"


def test_audit_trail_records_ops_actions(client):
    _seed_review_items(client)
    client.post("/api/admin/ops/entities/msp/msp1/status", headers=ADMIN,
                json={"action": "approve", "decided_by": "ops"})
    audit = client.get("/api/admin/ops/audit", headers=ADMIN).get_json()["audit"]
    assert any("/api/admin/ops/entities/msp/msp1/status" == a["path"] for a in audit)


def test_transition_unknown_entity_404(client):
    assert client.post("/api/admin/ops/entities/driver/ghost/status", headers=ADMIN,
                       json={"action": "approve", "decided_by": "x"}).status_code == 404
