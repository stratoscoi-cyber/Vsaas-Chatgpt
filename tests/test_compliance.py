"""Tests for the onboarding compliance engine and end-to-end flow."""

from datetime import date

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.marketplace import compliance
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base

AS_OF = date(2026, 6, 14)
FUTURE = "2027-01-01"
PAST = "2025-01-01"
RULE = {
    "required_driver_docs": ["drivers_license", "experience_proof"],
    "required_vehicle_docs": ["registration", "insurance", "roadworthiness"],
    "min_experience_years": 2.0, "min_insured_value": 1_000_000,
    "inspection_interval_days": 180, "require_tracker": True, "require_onboard_camera": True,
}
AGENCIES = {"DVLA", "AXA", "VIO"}


def _doc(t, issuer="DVLA", expiry=FUTURE, verified=True, **extra):
    return {"doc_type": t, "issuer_code": issuer, "expiry_on": expiry, "verified": verified, **extra}


# --- engine: driver ------------------------------------------------------------

def test_driver_no_rule_is_review():
    d = compliance.evaluate_driver({"experience_years": 3}, [], None, AGENCIES, AS_OF)
    assert d.decision == "review"


def test_driver_approved_when_complete():
    docs = [_doc("drivers_license"), _doc("experience_proof", issuer=None)]
    d = compliance.evaluate_driver({"experience_years": 3}, docs, RULE, AGENCIES, AS_OF)
    assert d.decision == "approved", d.reasons


def test_driver_rejected_low_experience():
    docs = [_doc("drivers_license"), _doc("experience_proof", issuer=None)]
    d = compliance.evaluate_driver({"experience_years": 1}, docs, RULE, AGENCIES, AS_OF)
    assert d.decision == "rejected" and any("experience" in r for r in d.reasons)


def test_driver_rejected_unapproved_issuer():
    docs = [_doc("drivers_license", issuer="FAKE"), _doc("experience_proof", issuer=None)]
    d = compliance.evaluate_driver({"experience_years": 3}, docs, RULE, AGENCIES, AS_OF)
    assert d.decision == "rejected" and any("issuer" in r for r in d.reasons)


def test_driver_rejected_expired_license():
    docs = [_doc("drivers_license", expiry=PAST), _doc("experience_proof", issuer=None)]
    d = compliance.evaluate_driver({"experience_years": 3}, docs, RULE, AGENCIES, AS_OF)
    assert d.decision == "rejected" and any("expired" in r for r in d.reasons)


# --- engine: vehicle -----------------------------------------------------------

def _vehicle(**over):
    base = {"vehicle_id": "v1", "owner_id": "o1", "tracker_serial": "T1",
            "tracker_approved": True, "tracker_serviceable": True, "camera_serial": "C1"}
    base.update(over)
    return base


def _vehicle_docs(insured=2_000_000, coverage=("vehicular", "load", "third_party")):
    return [_doc("registration", issuer="DVLA"),
            _doc("insurance", issuer="AXA", insured_value=insured, coverage=list(coverage)),
            _doc("roadworthiness", issuer="VIO")]


def _good_inspection():
    return {"result": "pass", "valid_until": FUTURE, "center_id": "vio-1"}


def test_vehicle_approved_when_complete():
    d = compliance.evaluate_vehicle(_vehicle(), _vehicle_docs(), _good_inspection(), RULE, AGENCIES, AS_OF)
    assert d.decision == "approved", d.reasons


def test_vehicle_rejected_no_tracker():
    d = compliance.evaluate_vehicle(_vehicle(tracker_serial=None), _vehicle_docs(),
                                    _good_inspection(), RULE, AGENCIES, AS_OF)
    assert d.decision == "rejected" and any("tracker" in r for r in d.reasons)


def test_vehicle_rejected_low_insured_value():
    d = compliance.evaluate_vehicle(_vehicle(), _vehicle_docs(insured=500), _good_inspection(),
                                    RULE, AGENCIES, AS_OF)
    assert d.decision == "rejected" and any("insured value" in r for r in d.reasons)


def test_vehicle_rejected_missing_coverage():
    d = compliance.evaluate_vehicle(_vehicle(), _vehicle_docs(coverage=("vehicular",)),
                                    _good_inspection(), RULE, AGENCIES, AS_OF)
    assert d.decision == "rejected" and any("load liability" in r for r in d.reasons)


def test_vehicle_rejected_expired_inspection():
    d = compliance.evaluate_vehicle(_vehicle(), _vehicle_docs(),
                                    {"result": "pass", "valid_until": PAST, "center_id": "vio-1"},
                                    RULE, AGENCIES, AS_OF)
    assert d.decision == "rejected" and any("inspection expired" in r for r in d.reasons)


def test_signals_downgrade_to_review():
    d = compliance.evaluate_vehicle(_vehicle(), _vehicle_docs(), _good_inspection(), RULE, AGENCIES, AS_OF)
    d = compliance.apply_signals(d, ["self_inspection"])
    assert d.decision == "review" and d.risk_score >= 0.5


# --- end-to-end flow -----------------------------------------------------------

@pytest.fixture
def client():
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    app = create_app(settings=Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False,
                     admin_api_keys=frozenset({"k"}), compliance_enforced=True), session_factory=factory)
    app.testing = True
    return app.test_client()


H = {"X-Admin-Key": "k"}


def _setup_config(client):
    for code, atype in [("DVLA", "licensing"), ("AXA", "insurance"), ("VIO", "inspection")]:
        client.post("/api/admin/agencies", headers=H,
                    json={"code": code, "name": code, "agency_type": atype, "region_code": "NG"})
    client.post("/api/admin/region-rules", headers=H, json={"region_code": "NG", **RULE})


def _doc_api(client, entity_type, entity_id, doc_type, **extra):
    return client.post("/api/compliance/documents",
                       json={"entity_type": entity_type, "entity_id": entity_id, "doc_type": doc_type,
                             "verified": True, "expiry_on": FUTURE, **extra})


def test_admin_config_requires_key(client):
    assert client.post("/api/admin/agencies", json={}).status_code == 403


def test_full_onboarding_approves_and_enables_offer(client):
    _setup_config(client)
    # driver
    client.post("/api/drivers", json={"driver_id": "d1", "region_code": "NG", "experience_years": 3,
                                      "owner_id": "o1"})
    _doc_api(client, "driver", "d1", "drivers_license", issuer_code="DVLA", reference="DL-1")
    _doc_api(client, "driver", "d1", "experience_proof", reference="EXP-1")
    assert client.post("/api/drivers/d1/submit").get_json()["status"] == "approved"
    # service center (different owner -> no self-inspection)
    client.post("/api/service-centers", json={"center_id": "vio-1", "owner_id": "co", "region_code": "NG"})
    # vehicle
    client.post("/api/vehicles", json={"vehicle_id": "v1", "owner_id": "o1", "vehicle_type": "truck_large",
                "capacity_kg": 20000, "region_code": "NG", "driver_id": "d1",
                "tracker_serial": "T1", "tracker_approved": True, "tracker_serviceable": True,
                "camera_serial": "C1", "features": []})
    _doc_api(client, "vehicle", "v1", "registration", issuer_code="DVLA", reference="REG-1")
    _doc_api(client, "vehicle", "v1", "insurance", issuer_code="AXA", reference="INS-1",
             insured_value=2_000_000, coverage=["vehicular", "load", "third_party"])
    _doc_api(client, "vehicle", "v1", "roadworthiness", issuer_code="VIO", reference="RW-1")
    client.post("/api/vehicles/v1/inspection", json={"center_id": "vio-1", "result": "pass",
                "valid_until": FUTURE, "performed_on": "2026-06-01"})
    assert client.post("/api/vehicles/v1/submit").get_json()["decision"] == "approved"

    # enforced offer now allowed
    load = client.post("/api/loads", json={"shipper_id": "s", "weight_kg": 8000,
                       "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    r = client.post(f"/api/loads/{load['ref']}/offers", json={"bidder_id": "o1", "bidder_role": "vehicle_owner",
                    "price": 1000, "vehicle_id": "v1"})
    assert r.status_code == 201


def test_enforcement_blocks_unapproved_vehicle(client):
    _setup_config(client)
    client.post("/api/vehicles", json={"vehicle_id": "v9", "owner_id": "o1", "vehicle_type": "truck_large",
                "capacity_kg": 20000, "region_code": "NG"})
    load = client.post("/api/loads", json={"shipper_id": "s", "weight_kg": 8000,
                       "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}).get_json()
    r = client.post(f"/api/loads/{load['ref']}/offers", json={"bidder_id": "o1", "bidder_role": "vehicle_owner",
                    "price": 1000, "vehicle_id": "v9"})
    assert r.status_code == 422 and r.get_json()["error"]["code"] == "compliance_blocked"


def test_self_inspection_forces_review(client):
    _setup_config(client)
    # service center owned by the same owner as the vehicle -> collusion signal
    client.post("/api/service-centers", json={"center_id": "vio-self", "owner_id": "o1", "region_code": "NG"})
    client.post("/api/vehicles", json={"vehicle_id": "v2", "owner_id": "o1", "vehicle_type": "truck_large",
                "capacity_kg": 20000, "region_code": "NG", "tracker_serial": "T", "tracker_approved": True,
                "tracker_serviceable": True, "camera_serial": "C"})
    _doc_api(client, "vehicle", "v2", "registration", issuer_code="DVLA", reference="REG-2")
    _doc_api(client, "vehicle", "v2", "insurance", issuer_code="AXA", reference="INS-2",
             insured_value=2_000_000, coverage=["vehicular", "load"])
    _doc_api(client, "vehicle", "v2", "roadworthiness", issuer_code="VIO", reference="RW-2")
    client.post("/api/vehicles/v2/inspection", json={"center_id": "vio-self", "result": "pass",
                "valid_until": FUTURE})
    res = client.post("/api/vehicles/v2/submit").get_json()
    assert res["decision"] == "review"
    assert "self_inspection" in res["signals"]

    # human override approves it
    ov = client.post(f"/api/admin/compliance/decisions/{res['id']}/override", headers=H,
                     json={"decision": "approved", "decided_by": "ops-admin", "note": "verified manually"})
    assert ov.status_code == 200 and ov.get_json()["decision"] == "approved"
    assert client.get("/api/vehicles").get_json()["vehicles"]  # sanity


def test_duplicate_document_flagged(client):
    _setup_config(client)
    client.post("/api/drivers", json={"driver_id": "dx", "region_code": "NG", "experience_years": 5})
    client.post("/api/drivers", json={"driver_id": "dy", "region_code": "NG", "experience_years": 5})
    # same DL reference on two drivers -> same hash -> duplicate signal
    _doc_api(client, "driver", "dx", "drivers_license", issuer_code="DVLA", reference="SHARED")
    _doc_api(client, "driver", "dx", "experience_proof", reference="EXPX")
    _doc_api(client, "driver", "dy", "drivers_license", issuer_code="DVLA", reference="SHARED")
    _doc_api(client, "driver", "dy", "experience_proof", reference="EXPY")
    res = client.post("/api/drivers/dy/submit").get_json()
    assert "duplicate_document" in res["risk"]["signals"]
