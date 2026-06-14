"""Load classification, vehicle matching and LTL consolidation.

Pure helpers (scope classification, FTL matching, LTL grouping) plus thin
DB-backed orchestration. Load modes:

* ``ftl``           -- full truckload; matched to a vehicle by type/tonnage.
* ``ltl``           -- less-than-truckload; a candidate for consolidation.
* ``consolidation`` -- an aggregate of LTL loads sharing origin/destination areas.

Scope: ``local`` (same region, short haul), ``intra_region`` (same region) or
``inter_region`` (across regions).
"""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from ..common.geo import haversine_km
from ..common.regions import region_for_coords
from .models import Consolidation, Load, Vehicle

LOCAL_KM = 50.0

# Special-handling classification -> required vehicle feature.
FEATURE_FOR_CLASS = {
    "refrigerated": "refrigeration",
    "hazardous": "hazmat_certified",
    "livestock": "livestock_rated",
    "oversized": "flatbed",
}


def classify_scope(origin_region: Optional[str], dest_region: Optional[str], distance_km: float) -> str:
    if origin_region and dest_region and origin_region == dest_region:
        return "local" if distance_km <= LOCAL_KM else "intra_region"
    if origin_region and dest_region and origin_region != dest_region:
        return "inter_region"
    # Unknown region(s): fall back to distance.
    return "local" if distance_km <= LOCAL_KM else "inter_region"


def classify_load(load: Load) -> Dict:
    """Derive regions, distance and scope for a load (pure on its fields)."""
    o_region = region_for_coords(load.origin)
    d_region = region_for_coords(load.destination)
    o_code = o_region.code if o_region else None
    d_code = d_region.code if d_region else None
    distance = load.distance_km
    if distance is None and load.origin and load.destination:
        distance = haversine_km(load.origin["lat"], load.origin["lon"],
                                load.destination["lat"], load.destination["lon"])
    return {
        "origin_region": o_code,
        "destination_region": d_code,
        "distance_km": distance,
        "scope": classify_scope(o_code, d_code, distance or 0.0),
    }


def vehicle_matches_load(load: Load, vehicle: Dict, *, require_approved: bool = True) -> Optional[Dict]:
    """Return a match descriptor if the vehicle can carry the load, else ``None``."""
    reasons = []
    if str(vehicle.get("available", "true")).lower() != "true":
        return None
    if require_approved and vehicle.get("compliance_status") != "approved":
        return None
    cap = vehicle.get("capacity_kg")
    if cap is not None and load.weight_kg and load.weight_kg > cap:
        return None
    features = set(vehicle.get("features") or [])
    for cls in (load.classifications or []):
        need = FEATURE_FOR_CLASS.get(cls)
        if need and need not in features:
            reasons.append(f"missing feature for {cls}")
    if reasons:
        return None
    utilisation = round((load.weight_kg or 0) / cap, 3) if cap else None
    return {"vehicle_id": vehicle.get("vehicle_id"), "vehicle_type": vehicle.get("vehicle_type"),
            "capacity_kg": cap, "utilisation": utilisation}


def match_vehicles(load: Load, vehicles: List[Dict], *, require_approved: bool = True) -> List[Dict]:
    """Rank matching vehicles: smallest sufficient capacity first (efficient fit)."""
    matches = [m for m in (vehicle_matches_load(load, v, require_approved=require_approved) for v in vehicles) if m]
    matches.sort(key=lambda m: (m["capacity_kg"] is None, m["capacity_kg"] or 0))
    return matches


def group_ltl(loads: List[Load], *, max_group_weight_kg: Optional[float] = None) -> List[Dict]:
    """Group LTL loads by (origin_region, destination_region) for consolidation."""
    buckets: Dict[tuple, List[Load]] = {}
    for ld in loads:
        key = (ld.origin_region, ld.destination_region)
        buckets.setdefault(key, []).append(ld)
    groups = []
    for (o, d), members in buckets.items():
        members = sorted(members, key=lambda x: x.ref)
        chunk, weight = [], 0.0
        for ld in members:
            w = ld.weight_kg or 0
            if max_group_weight_kg and chunk and weight + w > max_group_weight_kg:
                groups.append(_group(o, d, chunk, weight))
                chunk, weight = [], 0.0
            chunk.append(ld)
            weight += w
        if len(chunk) >= 2:  # a consolidation needs at least two loads
            groups.append(_group(o, d, chunk, weight))
    return groups


def _group(o, d, members, weight) -> Dict:
    return {"origin_region": o, "destination_region": d,
            "load_refs": [m.ref for m in members], "total_weight_kg": round(weight, 2),
            "count": len(members)}


# --- DB orchestration ----------------------------------------------------------

def matchable_vehicles(session, load: Load, *, require_approved: bool = True) -> List[Dict]:
    vehicles = [v.to_dict() for v in session.query(Vehicle).all()]
    return match_vehicles(load, vehicles, require_approved=require_approved)


def suggest_consolidations(session, *, region_code: Optional[str] = None,
                           max_group_weight_kg: Optional[float] = None) -> List[Dict]:
    q = session.query(Load).filter(Load.status == "open", Load.load_mode == "ltl")
    if region_code:
        q = q.filter(Load.origin_region == region_code)
    return group_ltl(q.all(), max_group_weight_kg=max_group_weight_kg)


def create_consolidation(session, load_refs: List[str]) -> Consolidation:
    loads = session.query(Load).filter(Load.ref.in_(load_refs)).all()
    if len(loads) < 2:
        raise ValueError("a consolidation needs at least two existing loads")
    o = loads[0].origin_region
    d = loads[0].destination_region
    cid = f"con_{uuid.uuid4().hex[:10]}"
    con = Consolidation(
        consolidation_id=cid, origin_region=o, destination_region=d,
        load_refs=[ld.ref for ld in loads],
        total_weight_kg=round(sum(ld.weight_kg or 0 for ld in loads), 2), status="open")
    session.add(con)
    for ld in loads:
        ld.consolidation_id = cid
        ld.load_mode = "consolidation"
    session.commit()
    return con
