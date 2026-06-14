"""Onboarding compliance rules engine (auto approve / fail / review).

Deterministic checks over **operator-configured** requirements (per-region rules
and the list of approved agencies) and **submitted** documents. The engine never
invents a verification: a document only counts when it is present, issued by an
approved agency, unexpired and marked verified. When the region's requirements
are not configured, the engine returns ``review`` (it will not auto-approve into
an unknown rule set).

Anti-collusion: rule checks decide approved/rejected; *risk signals* (computed
with cross-entity context, e.g. a vehicle inspected by a centre owned by the
vehicle's owner, or a re-used document) can downgrade an otherwise-approved
entity to ``review`` so a human checks it. This keeps human intervention limited
to the genuinely suspicious cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set

APPROVED, REJECTED, REVIEW = "approved", "rejected", "review"

# Risk weights for collusion signals; total >= REVIEW_RISK_THRESHOLD forces review.
SIGNAL_WEIGHTS = {
    "self_inspection": 0.6,        # inspected by a centre linked to the owner
    "duplicate_document": 0.6,     # a document re-used across entities
    "issuer_region_mismatch": 0.2,
    "inspection_velocity": 0.3,    # centre issuing an unusual number of passes
    "owner_many_rejections": 0.3,
}
REVIEW_RISK_THRESHOLD = 0.5


@dataclass
class Decision:
    decision: str
    reasons: List[str] = field(default_factory=list)     # hard failures
    warnings: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)     # collusion signals
    risk_score: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "decision": self.decision, "reasons": self.reasons, "warnings": self.warnings,
            "signals": self.signals, "risk_score": round(self.risk_score, 3),
        }


def _parse(d) -> Optional[date]:
    if not d:
        return None
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _docs_by_type(docs: List[Dict]) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for d in docs:
        out.setdefault(d.get("doc_type"), []).append(d)
    return out


def _check_document(
    doc: Dict, approved: Set[str], as_of: date, require_issuer: bool
) -> List[str]:
    problems = []
    if not doc.get("verified"):
        problems.append("not verified")
    expiry = _parse(doc.get("expiry_on"))
    if expiry and expiry < as_of:
        problems.append(f"expired on {expiry.isoformat()}")
    if require_issuer:
        issuer = doc.get("issuer_code")
        if not issuer or issuer not in approved:
            problems.append("issuer not in the approved list")
    return problems


# Document types whose issuer must be an approved agency.
ISSUER_REQUIRED = {"drivers_license", "permit", "insurance", "roadworthiness", "registration"}


def _evaluate_docs(required: List[str], docs: List[Dict], approved: Set[str], as_of: date):
    reasons, warnings = [], []
    by_type = _docs_by_type(docs)
    for doc_type in required:
        candidates = by_type.get(doc_type, [])
        if not candidates:
            reasons.append(f"missing required document: {doc_type}")
            continue
        # Pass if any submitted document of this type is fully valid.
        per_doc = [_check_document(c, approved, as_of, doc_type in ISSUER_REQUIRED) for c in candidates]
        if not any(p == [] for p in per_doc):
            reasons.append(f"invalid {doc_type}: {'; '.join(per_doc[0])}")
    return reasons, warnings


def evaluate_driver(driver: Dict, docs: List[Dict], rule: Optional[Dict],
                    approved_agencies: Set[str], as_of: Optional[date] = None) -> Decision:
    as_of = as_of or date.today()
    if not rule:
        return Decision(REVIEW, reasons=["no compliance rule configured for region"])
    reasons, warnings = _evaluate_docs(rule.get("required_driver_docs", []), docs, approved_agencies, as_of)
    min_years = float(rule.get("min_experience_years", 0) or 0)
    if float(driver.get("experience_years", 0) or 0) < min_years:
        reasons.append(f"driving experience below minimum of {min_years:g} years")
    return Decision(REJECTED if reasons else APPROVED, reasons=reasons, warnings=warnings)


def evaluate_vehicle(vehicle: Dict, docs: List[Dict], inspection: Optional[Dict],
                     rule: Optional[Dict], approved_agencies: Set[str],
                     as_of: Optional[date] = None) -> Decision:
    as_of = as_of or date.today()
    if not rule:
        return Decision(REVIEW, reasons=["no compliance rule configured for region"])
    reasons, warnings = _evaluate_docs(rule.get("required_vehicle_docs", []), docs, approved_agencies, as_of)

    # Insurance: acceptable insured value covering vehicular + load liability.
    min_insured = float(rule.get("min_insured_value", 0) or 0)
    insurance = [d for d in docs if d.get("doc_type") == "insurance" and not _check_document(d, approved_agencies, as_of, True)]
    if min_insured > 0:
        if not insurance:
            if "missing required document: insurance" not in reasons and \
               not any("insurance" in r for r in reasons):
                reasons.append("no valid insurance document")
        else:
            best = max(insurance, key=lambda d: d.get("insured_value") or 0)
            if (best.get("insured_value") or 0) < min_insured:
                reasons.append(f"insured value below minimum of {min_insured:g}")
            coverage = set(best.get("coverage") or [])
            for needed in ("vehicular", "load"):
                if needed not in coverage:
                    reasons.append(f"insurance does not cover {needed} liability")

    # IoT tracker / onboard camera.
    if rule.get("require_tracker", True):
        if not vehicle.get("tracker_serial"):
            reasons.append("no IoT tracker fitted")
        elif not (vehicle.get("tracker_approved") and vehicle.get("tracker_serviceable")):
            reasons.append("IoT tracker not approved/serviceable")
    if rule.get("require_onboard_camera", True) and not vehicle.get("camera_serial"):
        reasons.append("no onboard camera fitted")

    # Periodic physical/mechanical inspection.
    interval = rule.get("inspection_interval_days")
    if interval:
        if not inspection:
            reasons.append("no inspection on record")
        else:
            if inspection.get("result") != "pass":
                reasons.append("last inspection did not pass")
            valid_until = _parse(inspection.get("valid_until"))
            if valid_until and valid_until < as_of:
                reasons.append(f"inspection expired on {valid_until.isoformat()}")
            elif valid_until is None:
                warnings.append("inspection has no validity date")
    return Decision(REJECTED if reasons else APPROVED, reasons=reasons, warnings=warnings)


def evaluate_service_center(center: Dict, docs: List[Dict], rule: Optional[Dict],
                            approved_agencies: Set[str], as_of: Optional[date] = None) -> Decision:
    as_of = as_of or date.today()
    if not rule:
        return Decision(REVIEW, reasons=["no compliance rule configured for region"])
    # A service centre must hold an accreditation issued by an approved agency.
    reasons, warnings = _evaluate_docs(["accreditation"], docs, approved_agencies, as_of)
    return Decision(REJECTED if reasons else APPROVED, reasons=reasons, warnings=warnings)


def apply_signals(decision: Decision, signals: List[str]) -> Decision:
    """Fold collusion signals into a decision, downgrading approved -> review."""
    risk = sum(SIGNAL_WEIGHTS.get(s, 0.1) for s in signals)
    decision.signals = signals
    decision.risk_score = risk
    if decision.decision == APPROVED and risk >= REVIEW_RISK_THRESHOLD:
        decision.decision = REVIEW
        decision.warnings = decision.warnings + [f"auto-flagged for review (risk {risk:.2f})"]
    return decision
