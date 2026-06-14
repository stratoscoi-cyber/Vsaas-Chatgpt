"""Trust & safety: ops review queue and collusion-cluster analysis."""

from __future__ import annotations

from typing import Dict, List

from sqlalchemy import func

from .models import (
    ComplianceDecision, ComplianceDocument, Inspection, ServiceCenter, Vehicle,
)


def review_queue(session) -> List[Dict]:
    """Latest decision per entity that currently sits in 'review'."""
    latest_ids = (session.query(func.max(ComplianceDecision.id))
                  .group_by(ComplianceDecision.entity_type, ComplianceDecision.entity_id).all())
    ids = [r[0] for r in latest_ids]
    rows = (session.query(ComplianceDecision)
            .filter(ComplianceDecision.id.in_(ids), ComplianceDecision.decision == "review")
            .order_by(ComplianceDecision.risk_score.desc()).all())
    return [r.to_dict() for r in rows]


def collusion_clusters(session) -> List[Dict]:
    """Owners who both operate vehicles and control a service centre that has
    inspected one of those vehicles (a self-inspection ring), plus any shared
    documents across their entities."""
    clusters = []
    centers_by_owner: Dict[str, List[str]] = {}
    for c in session.query(ServiceCenter).all():
        if c.owner_id:
            centers_by_owner.setdefault(c.owner_id, []).append(c.center_id)

    for owner, center_ids in centers_by_owner.items():
        vehicles = session.query(Vehicle).filter_by(owner_id=owner).all()
        vehicle_ids = [v.vehicle_id for v in vehicles]
        if not vehicle_ids:
            continue
        self_inspections = (session.query(Inspection)
                            .filter(Inspection.vehicle_id.in_(vehicle_ids),
                                    Inspection.center_id.in_(center_ids)).count())
        if self_inspections:
            clusters.append({
                "owner_id": owner, "type": "self_inspection_ring",
                "service_centers": center_ids, "vehicles": vehicle_ids,
                "self_inspections": self_inspections,
            })

    # Shared documents across different entities.
    dupes = (session.query(ComplianceDocument.doc_hash, func.count(func.distinct(ComplianceDocument.entity_id)))
             .filter(ComplianceDocument.doc_hash.isnot(None))
             .group_by(ComplianceDocument.doc_hash)
             .having(func.count(func.distinct(ComplianceDocument.entity_id)) > 1).all())
    for doc_hash, n in dupes:
        entities = [d.entity_id for d in session.query(ComplianceDocument).filter_by(doc_hash=doc_hash).all()]
        clusters.append({"type": "shared_document", "doc_hash": doc_hash,
                         "entities": sorted(set(entities)), "count": int(n)})
    return clusters
