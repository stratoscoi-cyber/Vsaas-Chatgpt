"""TRAAS business logic: hazard handling, safety scoring and transit costing.

All functions here are pure so they can be unit-tested directly. The Flask layer
wires them to the database and routing engine.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from ..common.geo import buffer_point_ring, path_length_km

# How strongly each hazard class erodes a route's safety score (per hazard).
HAZARD_WEIGHTS = {
    "safety": 1.0,      # active kinetic events (banditry, kidnapping)
    "red_zone": 0.9,    # geopolitical / intelligence red zones
    "weather": 0.8,     # storms, hail, flooding
    "flood": 0.85,
    "traffic": 0.6,     # accidents, congestion
}
_DEFAULT_WEIGHT = 0.5

# Per-km cost multiplier relative to a baseline, by vehicle class.
VEHICLE_COST_FACTOR = {
    "heavy_truck": 1.0,
    "agricultural_vehicle": 0.75,
    "light_vehicle": 0.5,
    "drone_delivery": 0.25,
}
BASE_COST_PER_KM = 0.8
HAZARD_PENALTY = 20.0  # currency units per hazard encountered


def risk_level(safety_score: float) -> str:
    if safety_score < 0.3:
        return "critical"
    if safety_score < 0.6:
        return "high"
    if safety_score < 0.8:
        return "medium"
    return "low"


def route_safety(hazards: Sequence[Dict]) -> Dict:
    """Compute a 0..1 safety score and per-hazard breakdown for a route."""
    score = 1.0
    breakdown: List[Dict] = []
    for hazard in hazards:
        htype = hazard.get("hazard_type", "unknown")
        weight = HAZARD_WEIGHTS.get(htype, _DEFAULT_WEIGHT)
        score = max(0.0, score - weight * 0.1)
        breakdown.append(
            {
                "hazard_id": hazard.get("hazard_id"),
                "type": htype,
                "severity": hazard.get("severity"),
                "weight": weight,
            }
        )
    return {
        "safety_score": round(score, 3),
        "risk_level": risk_level(score),
        "hazards_on_route": breakdown,
    }


def transit_cost(distance_km: float, vehicle_type: str, hazard_count: int = 0) -> Dict:
    """Estimate transit cost for a vehicle class over a distance."""
    factor = VEHICLE_COST_FACTOR.get(vehicle_type, VEHICLE_COST_FACTOR["agricultural_vehicle"])
    base = distance_km * BASE_COST_PER_KM * factor
    penalty = hazard_count * HAZARD_PENALTY
    return {
        "base_cost": round(base, 2),
        "hazard_penalty": round(penalty, 2),
        "total_cost": round(base + penalty, 2),
        "vehicle_type": vehicle_type,
    }


def hazards_to_avoid_rings(hazards: Sequence[Dict]) -> List[List[List[float]]]:
    """Turn stored hazards into avoidance rings for the routing engine.

    A hazard with an explicit GeoJSON ``geometry`` (Polygon) contributes its
    outer ring directly; otherwise a circular ring is built from its point
    ``location`` and ``radius_km``.
    """
    rings: List[List[List[float]]] = []
    for hazard in hazards:
        geom = hazard.get("geometry")
        if geom and geom.get("type") == "Polygon" and geom.get("coordinates"):
            rings.append(geom["coordinates"][0])
            continue
        loc = hazard.get("location") or {}
        if loc.get("lat") is not None and loc.get("lon") is not None:
            rings.append(buffer_point_ring(loc["lat"], loc["lon"], hazard.get("radius_km", 2.0)))
    return rings


def route_distance_km(coords: Sequence) -> float:
    """Length in km of a path given as ``[lat, lon]`` pairs."""
    return round(path_length_km([(c[0], c[1]) for c in coords]), 3)
