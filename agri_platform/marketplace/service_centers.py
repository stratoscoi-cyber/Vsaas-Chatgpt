"""Enterprise service-station (service centre) onboarding workflow.

Combines regulatory compliance (per-region required documents from approved
agencies), a risk assessment (collusion, document re-use, anomalous pass rates,
poor reputation) and an explicit lifecycle:

    pending -> under_review -> approved | rejected | suspended

Decisions are auto-derived from real evidence; only ``under_review`` needs a human
(via the admin status transition), keeping manual work to the risky/ambiguous
cases. Reputation blends ratings with inspection performance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from . import compliance, compliance_service, reputation
from .models import ComplianceDecision, ComplianceDocument, Inspection, Rating, ServiceCenter, Vehicle

RISK_WEIGHTS = {
    "self_inspection_ring": 0.5,   # inspects vehicles owned by its own owner
    "duplicate_document": 0.4,     # documents re-used across entities
    "perfect_pass_rate": 0.3,      # never fails an inspection over high volume
    "poor_reputation": 0.3,        # low average rating
    "regulatory_gaps": 0.4,        # missing/invalid required documents
}
PERFECT_PASS_MIN_VOLUME = 20
POOR_RATING_THRESHOLD = 2.5
POOR_RATING_MIN_COUNT = 3
# Signals serious enough to force human review even at a moderate risk score.
REVIEW_FORCING_SIGNALS = {"self_inspection_ring", "duplicate_document"}


def risk_band(score: float) -> str:
    if score >= 0.85:
        return "critical"
    if score >= 0.6:
        return "high"
    if score >= 0.3:
        return "medium"
    return "low"


def inspection_performance(session, center_id: str) -> Dict:
    rows = session.query(Inspection).filter_by(center_id=center_id).all()
    total = len(rows)
    passes = sum(1 for r in rows if r.result == "pass")
    return {"inspections": total, "passes": passes, "fails": total - passes,
            "pass_rate": round(passes / total, 3) if total else None}


def risk_assessment(session, center: ServiceCenter, regulatory_reasons: Optional[List[str]] = None) -> Dict:
    signals: List[str] = []

    # Self-inspection ring: this centre inspected vehicles owned by its own owner.
    if center.owner_id:
        owned = [v.vehicle_id for v in session.query(Vehicle).filter_by(owner_id=center.owner_id).all()]
        if owned and session.query(Inspection).filter(
                Inspection.center_id == center.center_id, Inspection.vehicle_id.in_(owned)).count():
            signals.append("self_inspection_ring")

    # Re-used documents across entities.
    hashes = [d.doc_hash for d in session.query(ComplianceDocument)
              .filter_by(entity_type="service_center", entity_id=center.center_id).all() if d.doc_hash]
    if hashes and session.query(ComplianceDocument).filter(
            ComplianceDocument.doc_hash.in_(hashes),
            ComplianceDocument.entity_id != center.center_id).first():
        signals.append("duplicate_document")

    # Anomalous (perfect) pass rate over high volume.
    perf = inspection_performance(session, center.center_id)
    if perf["inspections"] >= PERFECT_PASS_MIN_VOLUME and perf["pass_rate"] == 1.0:
        signals.append("perfect_pass_rate")

    # Poor reputation.
    ratings = session.query(Rating).filter_by(subject_type="service_center", subject_id=center.center_id).all()
    if len(ratings) >= POOR_RATING_MIN_COUNT:
        avg = sum(r.score for r in ratings) / len(ratings)
        if avg < POOR_RATING_THRESHOLD:
            signals.append("poor_reputation")

    if regulatory_reasons:
        signals.append("regulatory_gaps")

    score = round(min(1.0, sum(RISK_WEIGHTS.get(s, 0.1) for s in signals)), 3)
    return {"risk_score": score, "risk_band": risk_band(score), "signals": signals,
            "performance": perf}


def submit(session, center: ServiceCenter) -> Dict:
    """Run regulatory + risk assessment and set the centre's lifecycle status."""
    rule = compliance_service.region_rule(session, center.region_code)
    codes = compliance_service.approved_codes(session, center.region_code)
    docs = [d.to_doc() for d in session.query(ComplianceDocument)
            .filter_by(entity_type="service_center", entity_id=center.center_id).all()]
    decision = compliance.evaluate_service_center(center.to_dict(), docs, rule, codes)

    risk = risk_assessment(session, center, decision.reasons)
    center.risk_score = risk["risk_score"]
    center.risk_band = risk["risk_band"]

    forced = bool(REVIEW_FORCING_SIGNALS.intersection(risk["signals"]))
    if decision.decision == compliance.REJECTED:
        status = "rejected"
    elif decision.decision == compliance.REVIEW or risk["risk_band"] in ("high", "critical") or forced:
        status = "under_review"
    else:
        status = "approved"
    center.compliance_status = status

    row = ComplianceDecision(
        entity_type="service_center", entity_id=center.center_id, decision=status,
        reasons=decision.reasons, warnings=decision.warnings, signals=risk["signals"],
        risk_score=risk["risk_score"], auto=True)
    session.add(row)
    session.commit()
    return {"center": center.to_dict(), "status": status, "decision": row.to_dict(), "risk": risk}


_TRANSITIONS = {
    "approve": "approved",
    "reject": "rejected",
    "suspend": "suspended",
    "reinstate": "approved",
}


def transition(session, center: ServiceCenter, action: str, *, reason: Optional[str] = None,
               decided_by: str) -> Dict:
    if action not in _TRANSITIONS:
        raise ValueError(f"invalid action '{action}'")
    new_status = _TRANSITIONS[action]
    center.compliance_status = new_status
    center.suspended_reason = reason if action == "suspend" else None
    row = ComplianceDecision(
        entity_type="service_center", entity_id=center.center_id, decision=new_status,
        reasons=[f"manual {action}"] + ([reason] if reason else []), warnings=[], signals=[],
        risk_score=center.risk_score or 0.0, auto=False, decided_by=decided_by)
    session.add(row)
    session.commit()
    return {"center": center.to_dict(), "decision": row.to_dict()}


def profile(session, center: ServiceCenter) -> Dict:
    return {
        "center": center.to_dict(),
        "reputation": reputation.reputation(session, "service_center", center.center_id),
        "performance": inspection_performance(session, center.center_id),
        "risk": {"risk_score": center.risk_score, "risk_band": center.risk_band},
    }
