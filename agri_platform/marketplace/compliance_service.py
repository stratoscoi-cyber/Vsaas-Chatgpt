"""LGaaS compliance orchestration.

Loads operator-configured requirements (region rules, approved agencies), the
entity's submitted documents and inspection, computes cross-entity anti-collusion
signals, runs the pure rules engine and records a decision. Most outcomes are
automatic; only genuinely suspicious or unconfigured cases land in ``review`` for
a human.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional, Set

from . import compliance
from .models import (
    ApprovedAgency, ComplianceDecision, ComplianceDocument, Driver, Inspection,
    RegionComplianceRule, ServiceCenter, Vehicle,
)

INSPECTION_VELOCITY_WINDOW_DAYS = 30
INSPECTION_VELOCITY_MAX = 50
OWNER_REJECTION_LIMIT = 3


def approved_codes(session, region_code: Optional[str]) -> Set[str]:
    q = session.query(ApprovedAgency).filter(ApprovedAgency.active.is_(True))
    rows = q.all()
    return {a.code for a in rows if a.region_code in (region_code, "*")}


def region_rule(session, region_code: Optional[str]) -> Optional[dict]:
    if not region_code:
        return None
    row = session.query(RegionComplianceRule).filter_by(region_code=region_code, active=True).first()
    return row.to_rule() if row else None


def _docs(session, entity_type: str, entity_id: str) -> List[dict]:
    rows = session.query(ComplianceDocument).filter_by(entity_type=entity_type, entity_id=entity_id).all()
    return [d.to_doc() for d in rows]


def _latest_inspection(session, vehicle_id: str) -> Optional[dict]:
    row = (session.query(Inspection).filter_by(vehicle_id=vehicle_id)
           .order_by(Inspection.id.desc()).first())
    return row.to_dict() if row else None


def _duplicate_document_signal(session, entity_type: str, entity_id: str) -> bool:
    hashes = [d.doc_hash for d in
              session.query(ComplianceDocument).filter_by(entity_type=entity_type, entity_id=entity_id).all()
              if d.doc_hash]
    if not hashes:
        return False
    clash = (session.query(ComplianceDocument)
             .filter(ComplianceDocument.doc_hash.in_(hashes))
             .filter(ComplianceDocument.entity_id != entity_id).first())
    return clash is not None


def _owner_rejection_signal(session, owner_id: Optional[str]) -> bool:
    if not owner_id:
        return False
    vehicle_ids = [v.vehicle_id for v in session.query(Vehicle).filter_by(owner_id=owner_id).all()]
    if not vehicle_ids:
        return False
    rejected = (session.query(ComplianceDecision)
                .filter(ComplianceDecision.entity_type == "vehicle",
                        ComplianceDecision.entity_id.in_(vehicle_ids),
                        ComplianceDecision.decision == "rejected").count())
    return rejected >= OWNER_REJECTION_LIMIT


def vehicle_signals(session, vehicle: Vehicle, inspection: Optional[dict]) -> List[str]:
    signals: List[str] = []
    if inspection and inspection.get("center_id"):
        center = session.query(ServiceCenter).filter_by(center_id=inspection["center_id"]).first()
        if center and center.owner_id and center.owner_id == vehicle.owner_id:
            signals.append("self_inspection")
        # Inspection velocity for that centre.
        since = (datetime.utcnow() - timedelta(days=INSPECTION_VELOCITY_WINDOW_DAYS)).date().isoformat()
        recent = (session.query(Inspection)
                  .filter(Inspection.center_id == inspection["center_id"],
                          Inspection.performed_on >= since,
                          Inspection.result == "pass").count())
        if recent > INSPECTION_VELOCITY_MAX:
            signals.append("inspection_velocity")
    if _duplicate_document_signal(session, "vehicle", vehicle.vehicle_id):
        signals.append("duplicate_document")
    if _owner_rejection_signal(session, vehicle.owner_id):
        signals.append("owner_many_rejections")
    return signals


def _record(session, entity_type, entity_id, decision: compliance.Decision,
            *, auto=True, decided_by=None) -> ComplianceDecision:
    row = ComplianceDecision(
        entity_type=entity_type, entity_id=entity_id, decision=decision.decision,
        reasons=decision.reasons, warnings=decision.warnings, signals=decision.signals,
        risk_score=decision.risk_score, auto=auto, decided_by=decided_by,
    )
    session.add(row)
    return row


def evaluate_driver(session, driver: Driver, *, as_of: Optional[date] = None) -> ComplianceDecision:
    rule = region_rule(session, driver.region_code)
    codes = approved_codes(session, driver.region_code)
    decision = compliance.evaluate_driver(
        {"experience_years": driver.experience_years}, _docs(session, "driver", driver.driver_id),
        rule, codes, as_of)
    signals = ["duplicate_document"] if _duplicate_document_signal(session, "driver", driver.driver_id) else []
    decision = compliance.apply_signals(decision, signals)
    driver.compliance_status = decision.decision
    row = _record(session, "driver", driver.driver_id, decision)
    session.commit()
    return row


def evaluate_vehicle(session, vehicle: Vehicle, *, as_of: Optional[date] = None) -> ComplianceDecision:
    rule = region_rule(session, vehicle.region_code)
    codes = approved_codes(session, vehicle.region_code)
    inspection = _latest_inspection(session, vehicle.vehicle_id)
    decision = compliance.evaluate_vehicle(
        vehicle.to_dict(), _docs(session, "vehicle", vehicle.vehicle_id), inspection, rule, codes, as_of)
    decision = compliance.apply_signals(decision, vehicle_signals(session, vehicle, inspection))
    vehicle.compliance_status = decision.decision
    if inspection and inspection.get("valid_until"):
        vehicle.inspection_valid_until = inspection["valid_until"]
    row = _record(session, "vehicle", vehicle.vehicle_id, decision)
    session.commit()
    return row


def evaluate_service_center(session, center: ServiceCenter, *, as_of: Optional[date] = None) -> ComplianceDecision:
    rule = region_rule(session, center.region_code)
    codes = approved_codes(session, center.region_code)
    decision = compliance.evaluate_service_center(
        center.to_dict(), _docs(session, "service_center", center.center_id), rule, codes, as_of)
    signals = ["duplicate_document"] if _duplicate_document_signal(session, "service_center", center.center_id) else []
    decision = compliance.apply_signals(decision, signals)
    center.compliance_status = decision.decision
    row = _record(session, "service_center", center.center_id, decision)
    session.commit()
    return row


def override(session, decision_id: int, new_decision: str, decided_by: str,
             note: Optional[str] = None) -> ComplianceDecision:
    prev = session.query(ComplianceDecision).filter_by(id=decision_id).first()
    if prev is None:
        raise LookupError("decision not found")
    row = ComplianceDecision(
        entity_type=prev.entity_type, entity_id=prev.entity_id, decision=new_decision,
        reasons=(["manual override"] + ([note] if note else [])), warnings=prev.warnings,
        signals=prev.signals, risk_score=prev.risk_score, auto=False, decided_by=decided_by)
    session.add(row)
    # Reflect on the entity.
    model = {"vehicle": Vehicle, "driver": Driver, "service_center": ServiceCenter}[prev.entity_type]
    id_col = {"vehicle": "vehicle_id", "driver": "driver_id", "service_center": "center_id"}[prev.entity_type]
    entity = session.query(model).filter_by(**{id_col: prev.entity_id}).first()
    if entity is not None:
        entity.compliance_status = new_decision
    session.commit()
    return row


def offer_eligibility(session, vehicle_id: Optional[str]) -> List[str]:
    """Return blocking reasons for using a vehicle on an offer (empty == allowed)."""
    if not vehicle_id:
        return ["vehicle_id is required when compliance is enforced"]
    vehicle = session.query(Vehicle).filter_by(vehicle_id=vehicle_id).first()
    if vehicle is None:
        return ["vehicle not found"]
    problems = []
    if vehicle.compliance_status != "approved":
        problems.append(f"vehicle not approved (status={vehicle.compliance_status or 'unverified'})")
    if vehicle.driver_id:
        driver = session.query(Driver).filter_by(driver_id=vehicle.driver_id).first()
        if driver is None or driver.compliance_status != "approved":
            problems.append("assigned driver not approved")
    else:
        problems.append("no approved driver assigned to the vehicle")
    return problems
