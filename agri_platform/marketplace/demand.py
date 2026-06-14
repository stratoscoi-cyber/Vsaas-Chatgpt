"""Dynamic pricing inputs: lane benchmarks, surge, backhaul and carbon.

All figures derive from real platform data (past awarded prices, current
open-load/available-vehicle balance) or standard reference emission factors —
nothing is invented.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..common.geo import haversine_km
from .models import Load, Offer, Vehicle

# Standard road-freight emission factors (g CO2e per tonne-km), by vehicle class.
# Reference range for HGV/road freight ~ 60–120 g/t-km; lighter vehicles higher
# per tonne. These are configurable defaults, not claims about a specific fleet.
EMISSION_FACTOR_G_PER_TONNE_KM = {
    "heavy_truck": 65.0,
    "truck_large": 65.0,
    "truck_small": 95.0,
    "agricultural_vehicle": 110.0,
    "light_vehicle": 140.0,
    "pickup": 160.0,
    "drone_delivery": 0.0,
}
DEFAULT_EMISSION_FACTOR = 90.0


def carbon_estimate(distance_km: float, weight_kg: float, vehicle_type: Optional[str] = None) -> Dict:
    factor = EMISSION_FACTOR_G_PER_TONNE_KM.get(vehicle_type or "", DEFAULT_EMISSION_FACTOR)
    tonnes = max(0.0, (weight_kg or 0) / 1000.0)
    grams = factor * tonnes * max(0.0, distance_km or 0)
    return {"vehicle_type": vehicle_type, "emission_factor_g_per_tonne_km": factor,
            "distance_km": round(distance_km or 0, 2), "tonnes": round(tonnes, 3),
            "co2e_kg": round(grams / 1000.0, 2)}


def lane_benchmark(session, origin_region: str, destination_region: str) -> Dict:
    """Average awarded price for a lane, from accepted offers on matching loads."""
    loads = session.query(Load).filter_by(origin_region=origin_region,
                                          destination_region=destination_region).all()
    refs = [l.ref for l in loads]
    if not refs:
        return {"samples": 0, "avg_price": None}
    accepted = session.query(Offer).filter(Offer.load_ref.in_(refs), Offer.status == "accepted").all()
    if not accepted:
        return {"samples": 0, "avg_price": None}
    prices = [o.price for o in accepted if o.price is not None]
    return {"samples": len(prices), "avg_price": round(sum(prices) / len(prices), 2) if prices else None,
            "min_price": min(prices) if prices else None, "max_price": max(prices) if prices else None}


def surge_factor(session, region_code: str) -> Dict:
    """Demand/supply balance in a region -> a 1.0..1.5 surge multiplier."""
    open_loads = session.query(Load).filter_by(origin_region=region_code, status="open").count()
    vehicles = session.query(Vehicle).filter_by(region_code=region_code).all()
    available = sum(1 for v in vehicles if str(v.available).lower() == "true"
                    and v.compliance_status in (None, "approved"))
    if available == 0:
        factor = 1.5 if open_loads > 0 else 1.0
    else:
        ratio = open_loads / available
        factor = max(1.0, min(1.5, 1.0 + 0.1 * ratio))
    return {"region_code": region_code, "open_loads": open_loads, "available_vehicles": available,
            "surge_factor": round(factor, 3)}


def backhaul_candidates(session, load: Load, *, radius_km: float = 75.0) -> List[Dict]:
    """Open loads forming a return trip: origin near this load's destination and
    destination near this load's origin."""
    if not load.origin or not load.destination:
        return []
    o, d = load.origin, load.destination
    out = []
    for cand in session.query(Load).filter(Load.status == "open", Load.ref != load.ref).all():
        if not cand.origin or not cand.destination:
            continue
        near_dest = haversine_km(d["lat"], d["lon"], cand.origin["lat"], cand.origin["lon"])
        near_origin = haversine_km(o["lat"], o["lon"], cand.destination["lat"], cand.destination["lon"])
        if near_dest <= radius_km and near_origin <= radius_km:
            out.append({"ref": cand.ref, "title": cand.title, "weight_kg": cand.weight_kg,
                        "pickup_km_from_dropoff": round(near_dest, 1),
                        "dropoff_km_from_origin": round(near_origin, 1)})
    return sorted(out, key=lambda x: x["pickup_km_from_dropoff"])
