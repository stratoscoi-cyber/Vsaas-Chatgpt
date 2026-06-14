"""Operations console aggregation across every onboarding entity type.

One risk-ranked review queue, a status/risk dashboard, an audit view and a single
status-transition action that works for drivers, vehicles, service centres and
parties (operators, fleet managers, inspection agents, MSPs). It reads the shared
``ComplianceDecision`` history and the per-entity risk fields — no new data, just
a unified view and control surface for the ops team.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import func

from . import onboarding
from .models import (
    AuditLog, ComplianceDecision, Driver, Party, ServiceCenter, Vehicle,
)

REVIEW_STATUSES = {"review", "under_review"}
_VALID_ACTIONS = {"approve": "approved", "reject": "rejected", "suspend": "suspended",
                  "reinstate": "approved"}


def resolve_entity(session, entity_type: str, entity_id: str):
    """Return the ORM object for an entity reference, or ``None``."""
    if entity_type == "driver":
        return session.query(Driver).filter_by(driver_id=entity_id).first()
    if entity_type == "vehicle":
        return session.query(Vehicle).filter_by(vehicle_id=entity_id).first()
    if entity_type == "service_center":
        return session.query(ServiceCenter).filter_by(center_id=entity_id).first()
    if entity_type in onboarding.PARTY_ROLES:
        return session.query(Party).filter_by(party_id=entity_id, party_type=entity_type).first()
    return None


def _entity_snapshot(session, entity_type: str, entity_id: str) -> Dict:
    obj = resolve_entity(session, entity_type, entity_id)
    if obj is None:
        return {"name": None, "compliance_status": None, "risk_band": None}
    return {"name": getattr(obj, "name", None),
            "compliance_status": getattr(obj, "compliance_status", None),
            "risk_band": getattr(obj, "risk_band", None),
            "region_code": getattr(obj, "region_code", None)}


def _latest_decision_ids(session) -> List[int]:
    rows = (session.query(func.max(ComplianceDecision.id))
            .group_by(ComplianceDecision.entity_type, ComplianceDecision.entity_id).all())
    return [r[0] for r in rows]


def review_queue(session, entity_type: Optional[str] = None) -> List[Dict]:
    """Latest decision per entity that currently sits in review, risk-ranked."""
    ids = _latest_decision_ids(session)
    q = (session.query(ComplianceDecision)
         .filter(ComplianceDecision.id.in_(ids), ComplianceDecision.decision.in_(REVIEW_STATUSES)))
    if entity_type:
        q = q.filter(ComplianceDecision.entity_type == entity_type)
    rows = q.order_by(ComplianceDecision.risk_score.desc().nullslast(), ComplianceDecision.id.desc()).all()
    out = []
    for d in rows:
        item = d.to_dict()
        item["entity"] = _entity_snapshot(session, d.entity_type, d.entity_id)
        out.append(item)
    return out


def dashboard(session) -> Dict:
    """Counts by status and entity type, plus the high-risk roster."""
    ids = _latest_decision_ids(session)
    latest = session.query(ComplianceDecision).filter(ComplianceDecision.id.in_(ids)).all()
    by_status: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    for d in latest:
        by_status[d.decision] = by_status.get(d.decision, 0) + 1
        by_type[d.entity_type] = by_type.get(d.entity_type, 0) + 1

    high_risk = []
    # Models carrying a risk band (vehicles use compliance_status without a band).
    for model, id_attr, etype_const in (
        (Driver, "driver_id", "driver"),
        (ServiceCenter, "center_id", "service_center"),
        (Party, "party_id", None),
    ):
        for obj in session.query(model).filter(model.risk_band.in_(("high", "critical"))).all():
            high_risk.append({"entity_type": etype_const or getattr(obj, "party_type", "party"),
                              "entity_id": getattr(obj, id_attr), "name": getattr(obj, "name", None),
                              "risk_band": obj.risk_band, "risk_score": obj.risk_score,
                              "compliance_status": obj.compliance_status})
    high_risk.sort(key=lambda x: (x["risk_score"] or 0), reverse=True)
    return {"review_count": len(review_queue(session)),
            "by_status": by_status, "by_entity_type": by_type, "high_risk": high_risk}


def decision_history(session, entity_type: str, entity_id: str) -> List[Dict]:
    rows = (session.query(ComplianceDecision)
            .filter_by(entity_type=entity_type, entity_id=entity_id)
            .order_by(ComplianceDecision.id.desc()).all())
    return [r.to_dict() for r in rows]


def entity_profile(session, entity_type: str, entity_id: str) -> Optional[Dict]:
    obj = resolve_entity(session, entity_type, entity_id)
    if obj is None:
        return None
    return {"entity_type": entity_type, "entity_id": entity_id, "entity": obj.to_dict(),
            "decisions": decision_history(session, entity_type, entity_id)}


def audit(session, limit: int = 100) -> List[Dict]:
    rows = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500)).all()
    return [r.to_dict() for r in rows]


def transition(session, entity_type: str, entity_id: str, action: str, *, reason: Optional[str],
               decided_by: str) -> Dict:
    """Apply a status transition uniformly to any onboarding entity."""
    if action not in _VALID_ACTIONS:
        raise ValueError(f"invalid action '{action}'")
    obj = resolve_entity(session, entity_type, entity_id)
    if obj is None:
        raise LookupError("entity not found")
    new_status = _VALID_ACTIONS[action]
    obj.compliance_status = new_status
    if hasattr(obj, "suspended_reason"):
        obj.suspended_reason = reason if action == "suspend" else None
    row = ComplianceDecision(entity_type=entity_type, entity_id=entity_id, decision=new_status,
                             reasons=[f"manual {action}"] + ([reason] if reason else []),
                             warnings=[], signals=[], risk_score=getattr(obj, "risk_score", 0.0) or 0.0,
                             auto=False, decided_by=decided_by)
    session.add(row)
    session.commit()
    return {"entity_type": entity_type, "entity_id": entity_id, "status": new_status,
            "decision": row.to_dict()}
