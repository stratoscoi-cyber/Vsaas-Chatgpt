"""WFAAS business logic.

The hazard *evaluators* are pure functions over already-fetched values so they
can be unit-tested without any network or database. The persistence helpers take
an explicit SQLAlchemy session, keeping side effects easy to control and test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .models import Drone, DroneMission, WeatherAlert

# Canonical alert type identifiers.
ALERT_TYPES = {
    "WEATHER_HAZARD": "weather_hazard",
    "SOIL_CONDITION": "soil_condition",
    "CROP_HEALTH": "crop_health",
    "DRONE_MONITORING": "drone_monitoring",
}

# Confidence is intentionally *inverse* to severity: a Critical alert is acted on
# even when our confidence it is a true positive is comparatively low, whereas a
# Low alert is only surfaced when we are fairly sure.
_SEVERITY_CONFIDENCE = {"Low": 0.8, "Medium": 0.6, "High": 0.4, "Critical": 0.2}


@dataclass(frozen=True)
class HazardThresholds:
    precip_prob_high: float = 80.0       # %
    wind_speed_high: float = 20.0        # m/s
    soil_moisture_critical: float = 0.10  # m3/m3
    soil_moisture_low: float = 0.20       # m3/m3


DEFAULT_THRESHOLDS = HazardThresholds()


def alert_confidence(severity: str) -> float:
    return _SEVERITY_CONFIDENCE.get(severity, 0.5)


def evaluate_weather_hazard(
    precip_probs: Sequence[float],
    wind_speeds: Sequence[float],
    thresholds: HazardThresholds = DEFAULT_THRESHOLDS,
) -> Optional[Dict]:
    """Return an alert spec for precipitation/wind hazards, or ``None``."""
    max_precip = max((p for p in precip_probs if p is not None), default=0)
    max_wind = max((w for w in wind_speeds if w is not None), default=0)
    if max_precip >= thresholds.precip_prob_high:
        return {
            "alert_type": ALERT_TYPES["WEATHER_HAZARD"],
            "severity": "High",
            "message": f"High precipitation probability ({max_precip:.0f}%) forecast",
            "related_data": {"max_precipitation_probability": max_precip},
        }
    if max_wind >= thresholds.wind_speed_high:
        return {
            "alert_type": ALERT_TYPES["WEATHER_HAZARD"],
            "severity": "High",
            "message": f"High winds ({max_wind:.1f} m/s) forecast",
            "related_data": {"max_wind_speed": max_wind},
        }
    return None


def evaluate_soil_condition(
    soil_moisture: Sequence[float],
    thresholds: HazardThresholds = DEFAULT_THRESHOLDS,
) -> Optional[Dict]:
    """Return an alert spec for dry-soil conditions, or ``None``."""
    values = [v for v in soil_moisture if v is not None]
    if not values:
        return None
    current = values[-1]
    if current < thresholds.soil_moisture_critical:
        severity = "Critical"
        label = "Critical soil moisture deficit"
    elif current < thresholds.soil_moisture_low:
        severity = "High"
        label = "Dry soil conditions"
    else:
        return None
    return {
        "alert_type": ALERT_TYPES["SOIL_CONDITION"],
        "severity": severity,
        "message": f"{label} (soil moisture {current:.3f} m3/m3)",
        "related_data": {"soil_moisture": current, "units": "m3/m3"},
    }


# --- persistence helpers -------------------------------------------------------

def create_alert(
    session,
    farm_id: str,
    spec: Dict,
    coordinates: Dict[str, float],
) -> WeatherAlert:
    """Persist an alert described by an evaluator ``spec``."""
    alert = WeatherAlert(
        farm_id=farm_id,
        alert_type=spec["alert_type"],
        severity=spec["severity"],
        message=spec["message"],
        coordinates=coordinates,
        timestamp=datetime.utcnow(),
        resolved=False,
        related_data=spec.get("related_data"),
        confidence=alert_confidence(spec["severity"]),
    )
    session.add(alert)
    session.commit()
    return alert


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def schedule_drone_mission(
    session,
    farm_id: str,
    mission_type: str,
    waypoints: List[List[float]],
    payload: Optional[Dict] = None,
    drone_id: Optional[str] = None,
) -> DroneMission:
    """Assign an available drone and create a pending mission.

    Raises ``LookupError`` when no suitable drone is free.
    """
    query = session.query(Drone).filter(Drone.status == "available")
    if drone_id:
        query = query.filter(Drone.drone_id == drone_id)
    drone = query.first()
    if drone is None:
        raise LookupError("no available drone for mission")

    mission = DroneMission(
        mission_id=_new_id("m"),
        farm_id=farm_id,
        drone_id=drone.drone_id,
        status="in_progress",
        mission_type=mission_type,
        waypoints=waypoints,
        payload=payload or {},
        start_time=datetime.utcnow(),
    )
    drone.status = "in_mission"
    session.add(mission)
    session.commit()
    return mission


def complete_drone_mission(session, mission_id: str, results: Dict) -> DroneMission:
    """Mark a mission complete, store results, and free its drone."""
    mission = session.query(DroneMission).filter_by(mission_id=mission_id).first()
    if mission is None:
        raise LookupError(f"mission {mission_id} not found")
    mission.status = "completed"
    mission.results = results
    mission.end_time = datetime.utcnow()
    drone = session.query(Drone).filter_by(drone_id=mission.drone_id).first()
    if drone is not None:
        drone.status = "available"
    session.commit()
    return mission
