"""Fleet & driver capacity: inspection service-due and hours-of-service."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Optional

from .models import DutyLog, Vehicle

# Hours-of-service: a common ceiling is ~11h driving in a rolling 24h window.
MAX_DRIVING_HOURS_24H = 11.0


def service_status(session, vehicle: Vehicle, *, as_of: Optional[date] = None) -> Dict:
    as_of = as_of or date.today()
    valid_until = vehicle.inspection_valid_until
    days_to_due = None
    overdue = False
    if valid_until:
        try:
            d = date.fromisoformat(valid_until)
            days_to_due = (d - as_of).days
            overdue = days_to_due < 0
        except ValueError:
            pass
    return {"vehicle_id": vehicle.vehicle_id, "inspection_valid_until": valid_until,
            "days_to_due": days_to_due, "overdue": overdue,
            "telematics_status": vehicle.telematics_status, "odometer_km": vehicle.odometer_km}


def log_duty(session, driver_id: str, status: str, *, at: Optional[datetime] = None) -> DutyLog:
    if status not in ("driving", "on_duty", "rest", "off"):
        raise ValueError("invalid duty status")
    at = at or datetime.utcnow()
    # Close the previous open segment.
    prev = (session.query(DutyLog).filter_by(driver_id=driver_id, ended_at=None)
            .order_by(DutyLog.id.desc()).first())
    if prev is not None:
        prev.ended_at = at
    seg = DutyLog(driver_id=driver_id, status=status, started_at=at)
    session.add(seg)
    session.commit()
    return seg


def driving_hours(session, driver_id: str, *, window_hours: int = 24,
                  as_of: Optional[datetime] = None) -> Dict:
    as_of = as_of or datetime.utcnow()
    since = as_of - timedelta(hours=window_hours)
    segments = session.query(DutyLog).filter_by(driver_id=driver_id).all()
    total = 0.0
    for s in segments:
        if s.status != "driving":
            continue
        start = max(s.started_at, since)
        end = s.ended_at or as_of
        if end > start:
            total += (end - start).total_seconds() / 3600.0
    total = round(total, 2)
    return {"driver_id": driver_id, "window_hours": window_hours, "driving_hours": total,
            "limit_hours": MAX_DRIVING_HOURS_24H, "remaining_hours": round(max(0.0, MAX_DRIVING_HOURS_24H - total), 2),
            "compliant": total <= MAX_DRIVING_HOURS_24H}
