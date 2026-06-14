"""Carrier/driver reputation from ratings and delivery performance."""

from __future__ import annotations

from typing import Dict, Optional

from .models import Inspection, Rating, Shipment


def add_rating(session, *, subject_type: str, subject_id: str, score: float,
               rater_id: Optional[str] = None, shipment_id: Optional[str] = None,
               comment: Optional[str] = None) -> Rating:
    if not (1 <= float(score) <= 5):
        raise ValueError("score must be between 1 and 5")
    rating = Rating(subject_type=subject_type, subject_id=subject_id, score=float(score),
                    rater_id=rater_id, shipment_id=shipment_id, comment=comment)
    session.add(rating)
    session.commit()
    return rating


def performance(session, carrier_id: str) -> Dict:
    """Delivery performance counters for a carrier (from shipments)."""
    shipments = session.query(Shipment).filter_by(carrier_id=carrier_id).all()
    completed = [s for s in shipments if s.status in ("arrived", "delivered")]
    delivered = [s for s in shipments if s.status == "delivered"]
    delayed = [s for s in shipments if (s.delay_minutes or 0) > 0]
    cancelled = [s for s in shipments if s.status == "cancelled"]
    on_time = [s for s in delivered if (s.delay_minutes or 0) == 0]
    total = len(shipments)
    return {
        "shipments": total,
        "completed": len(completed),
        "delivered": len(delivered),
        "on_time": len(on_time),
        "delayed": len(delayed),
        "cancelled": len(cancelled),
        "on_time_rate": round(len(on_time) / len(delivered), 3) if delivered else None,
        "cancellation_rate": round(len(cancelled) / total, 3) if total else None,
    }


def reputation(session, subject_type: str, subject_id: str) -> Dict:
    ratings = session.query(Rating).filter_by(subject_type=subject_type, subject_id=subject_id).all()
    avg = round(sum(r.score for r in ratings) / len(ratings), 2) if ratings else None
    result = {"subject_type": subject_type, "subject_id": subject_id,
              "ratings_count": len(ratings), "avg_score": avg}
    if subject_type == "carrier":
        perf = performance(session, subject_id)
        result["performance"] = perf
        result["score"] = _composite(avg, perf)
    elif subject_type == "service_center":
        rows = session.query(Inspection).filter_by(center_id=subject_id).all()
        passes = sum(1 for r in rows if r.result == "pass")
        result["performance"] = {"inspections": len(rows), "passes": passes,
                                 "pass_rate": round(passes / len(rows), 3) if rows else None}
    return result


def _composite(avg_score: Optional[float], perf: Dict) -> Optional[float]:
    """A 0..1 trust score blending ratings, on-time and cancellation behaviour."""
    parts, weights = [], []
    if avg_score is not None:
        parts.append(avg_score / 5.0); weights.append(0.5)
    if perf.get("on_time_rate") is not None:
        parts.append(perf["on_time_rate"]); weights.append(0.3)
    if perf.get("cancellation_rate") is not None:
        parts.append(1.0 - perf["cancellation_rate"]); weights.append(0.2)
    if not parts:
        return None
    return round(sum(p * w for p, w in zip(parts, weights)) / sum(weights), 3)
