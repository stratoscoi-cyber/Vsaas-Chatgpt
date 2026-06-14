"""Unified, admin-managed onboarding engine for people & organisations.

Covers drivers, logistics operators, fleet managers, platform inspection agents
and MSPs (managed service providers) with one deterministic workflow:

    pending -> under_review -> approved | rejected | suspended

Requirements are **admin-configured per (region, role)** (`RoleRequirement`):
required documents (issued by approved agencies), minimum experience, and whether
sanctions/PEP **screening** is required. Screening uses the configured KYC
provider — the default verifies/clears nothing, so an unscreened party with a
screening requirement lands in review (never auto-approved). Risk signals
(re-used documents, screening hits, poor reputation, conflicts of interest) route
otherwise-clean parties to human review. No fabricated verifications or rates.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from . import compliance, compliance_service, reputation
from .models import ComplianceDecision, ComplianceDocument, KycCheck, Rating, Vehicle

PARTY_ROLES = ("logistics_operator", "fleet_manager", "inspection_agent", "msp")
ALL_ROLES = ("driver",) + PARTY_ROLES
ONBOARDING_ENTITY_TYPES = ("driver", "vehicle", "service_center") + PARTY_ROLES

RISK_WEIGHTS = {
    "duplicate_document": 0.4,
    "screening_hit": 0.9,
    "screening_unconfirmed": 0.3,
    "poor_reputation": 0.3,
    "conflict_of_interest": 0.5,   # inspection agent linked to vehicles it would inspect
    "regulatory_gaps": 0.4,
}
REVIEW_FORCING_SIGNALS = {"duplicate_document", "conflict_of_interest", "screening_unconfirmed"}
POOR_RATING_THRESHOLD = 2.5
POOR_RATING_MIN_COUNT = 3


def risk_band(score: float) -> str:
    if score >= 0.85:
        return "critical"
    if score >= 0.6:
        return "high"
    if score >= 0.3:
        return "medium"
    return "low"


def requirement(session, region_code: Optional[str], role: str) -> Optional[dict]:
    """Admin requirement for a (region, role).

    Drivers reuse the existing :class:`RegionComplianceRule` so prior driver
    configuration keeps working; the other roles use :class:`RoleRequirement`.
    """
    from .models import RoleRequirement
    if not region_code:
        return None
    if role == "driver":
        rule = compliance_service.region_rule(session, region_code)
        if not rule:
            return None
        return {"required_docs": rule.get("required_driver_docs", []),
                "required_screening": rule.get("require_driver_screening", False),
                "min_experience_years": rule.get("min_experience_years", 0)}
    row = session.query(RoleRequirement).filter_by(region_code=region_code, role=role, active=True).first()
    return row.to_rule() if row else None


def _docs(session, role: str, entity_id: str) -> List[dict]:
    rows = session.query(ComplianceDocument).filter_by(entity_type=role, entity_id=entity_id).all()
    return [d.to_doc() for d in rows]


def _duplicate_document(session, role: str, entity_id: str) -> bool:
    hashes = [d.doc_hash for d in
              session.query(ComplianceDocument).filter_by(entity_type=role, entity_id=entity_id).all()
              if d.doc_hash]
    if not hashes:
        return False
    return session.query(ComplianceDocument).filter(
        ComplianceDocument.doc_hash.in_(hashes),
        ComplianceDocument.entity_id != entity_id).first() is not None


def evaluate(session, *, role: str, entity_id: str, region_code: Optional[str],
             experience_years: float, as_of: Optional[date] = None):
    as_of = as_of or date.today()
    req = requirement(session, region_code, role)
    if not req:
        return compliance.Decision(compliance.REVIEW,
                                   reasons=[f"no requirement configured for region/{role}"]), req
    codes = compliance_service.approved_codes(session, region_code)
    reasons, warnings = compliance._evaluate_docs(req["required_docs"], _docs(session, role, entity_id), codes, as_of)
    min_exp = float(req.get("min_experience_years", 0) or 0)
    if min_exp and float(experience_years or 0) < min_exp:
        reasons.append(f"experience below minimum of {min_exp:g} years")
    return compliance.Decision(compliance.REJECTED if reasons else compliance.APPROVED,
                               reasons=reasons, warnings=warnings), req


def risk_assessment(session, *, role: str, entity_id: str, owner_id: Optional[str],
                    reasons: List[str], screening_clear: Optional[bool],
                    screening_required: bool) -> Dict:
    signals: List[str] = []
    if _duplicate_document(session, role, entity_id):
        signals.append("duplicate_document")
    if screening_clear is False:
        signals.append("screening_hit")
    elif screening_required and screening_clear is None:
        signals.append("screening_unconfirmed")
    # Inspection agents must be independent of the fleets they inspect.
    if role == "inspection_agent" and owner_id and \
            session.query(Vehicle).filter_by(owner_id=owner_id).first() is not None:
        signals.append("conflict_of_interest")
    ratings = session.query(Rating).filter_by(subject_type=role, subject_id=entity_id).all()
    if len(ratings) >= POOR_RATING_MIN_COUNT and sum(r.score for r in ratings) / len(ratings) < POOR_RATING_THRESHOLD:
        signals.append("poor_reputation")
    if reasons:
        signals.append("regulatory_gaps")
    score = round(min(1.0, sum(RISK_WEIGHTS.get(s, 0.1) for s in signals)), 3)
    return {"risk_score": score, "risk_band": risk_band(score), "signals": signals}


def submit(session, subject, *, role: str, entity_id: str, region_code: Optional[str],
           name: Optional[str], experience_years: float, owner_id: Optional[str],
           kyc_provider) -> Dict:
    decision, req = evaluate(session, role=role, entity_id=entity_id, region_code=region_code,
                             experience_years=experience_years)

    screening_status, clear = None, None
    if req and req.get("required_screening"):
        res = kyc_provider.screen({"name": name, "entity_type": role, "entity_id": entity_id})
        screening_status, clear = res.status, res.clear
        session.add(KycCheck(entity_type=role, entity_id=entity_id, kind="screening",
                             provider=getattr(kyc_provider, "name", "manual"),
                             verified=clear, status=res.status, details={"hits": res.hits}))
        if clear is False:
            decision.decision = compliance.REJECTED
            decision.reasons = decision.reasons + ["failed sanctions/PEP screening"]

    risk = risk_assessment(session, role=role, entity_id=entity_id, owner_id=owner_id,
                           reasons=decision.reasons, screening_clear=clear,
                           screening_required=bool(req and req.get("required_screening")))

    forced = bool(REVIEW_FORCING_SIGNALS.intersection(risk["signals"]))
    if decision.decision == compliance.REJECTED:
        status = "rejected"
    elif decision.decision == compliance.REVIEW or risk["risk_band"] in ("high", "critical") or forced:
        status = "under_review"
    else:
        status = "approved"

    subject.compliance_status = status
    subject.risk_score = risk["risk_score"]
    subject.risk_band = risk["risk_band"]
    subject.screening_status = screening_status
    row = ComplianceDecision(entity_type=role, entity_id=entity_id, decision=status,
                             reasons=decision.reasons, warnings=decision.warnings,
                             signals=risk["signals"], risk_score=risk["risk_score"], auto=True)
    session.add(row)
    session.commit()
    return {"status": status, "decision": row.to_dict(), "risk": risk,
            "screening_status": screening_status}


_TRANSITIONS = {"approve": "approved", "reject": "rejected", "suspend": "suspended",
                "reinstate": "approved"}


def transition(session, subject, *, role: str, entity_id: str, action: str,
               reason: Optional[str], decided_by: str) -> Dict:
    if action not in _TRANSITIONS:
        raise ValueError(f"invalid action '{action}'")
    new_status = _TRANSITIONS[action]
    subject.compliance_status = new_status
    if hasattr(subject, "suspended_reason"):
        subject.suspended_reason = reason if action == "suspend" else None
    row = ComplianceDecision(entity_type=role, entity_id=entity_id, decision=new_status,
                             reasons=[f"manual {action}"] + ([reason] if reason else []),
                             warnings=[], signals=[], risk_score=getattr(subject, "risk_score", 0.0) or 0.0,
                             auto=False, decided_by=decided_by)
    session.add(row)
    session.commit()
    return {"status": new_status, "decision": row.to_dict()}


def profile(session, subject, *, role: str, entity_id: str) -> Dict:
    return {"party": subject.to_dict(),
            "reputation": reputation.reputation(session, role, entity_id),
            "risk": {"risk_score": getattr(subject, "risk_score", None),
                     "risk_band": getattr(subject, "risk_band", None),
                     "screening_status": getattr(subject, "screening_status", None)}}
