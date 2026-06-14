"""Freight rate estimation.

Produces a recommended price and a low/high band from chargeable weight
(the greater of actual and volumetric weight), distance, load type, special
handling classifications and urgency. The model is transparent and deterministic
so quotes are explainable to both shippers and carriers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from ..common.geo import haversine_km

# Road-freight volumetric divisor (cm^3 per kg). 3000–5000 is typical for road;
# 4000 is a reasonable middle ground.
VOLUMETRIC_DIVISOR_CM3_PER_KG = 4000.0

# Straight-line distance underestimates road distance; scale it up.
ROAD_DISTANCE_FACTOR = 1.3

# Base economics.
BASE_RATE_PER_TONNE_KM = 0.12   # currency units per tonne per km
MIN_CHARGE = 25.0
BAND_SPREAD = 0.15              # +/- around the recommended price

# Load-type multipliers (handling difficulty / value at risk).
LOAD_TYPE_MULTIPLIER = {
    "general": 1.0,
    "grain": 1.0,
    "fertilizer": 1.1,
    "equipment": 1.2,
    "perishable_produce": 1.25,
    "fuel": 1.5,
    "livestock": 1.4,
}

# Additive surcharges (fractions) for special handling classifications.
CLASSIFICATION_SURCHARGE = {
    "refrigerated": 0.25,
    "hazardous": 0.40,
    "fragile": 0.15,
    "oversized": 0.30,
    "livestock": 0.20,
    "express": 0.20,
}


@dataclass(frozen=True)
class RateEstimate:
    distance_km: float
    chargeable_weight_kg: float
    load_multiplier: float
    surcharge_fraction: float
    urgency_factor: float
    recommended: float
    low: float
    high: float
    currency: str
    breakdown: Dict

    def to_dict(self) -> Dict:
        return {
            "distance_km": round(self.distance_km, 2),
            "chargeable_weight_kg": round(self.chargeable_weight_kg, 2),
            "load_multiplier": self.load_multiplier,
            "surcharge_fraction": round(self.surcharge_fraction, 3),
            "urgency_factor": self.urgency_factor,
            "recommended": round(self.recommended, 2),
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "currency": self.currency,
            "breakdown": self.breakdown,
        }


def volumetric_weight_kg(dimensions_cm: Optional[Dict]) -> float:
    if not dimensions_cm:
        return 0.0
    l = float(dimensions_cm.get("length", 0) or 0)
    w = float(dimensions_cm.get("width", 0) or 0)
    h = float(dimensions_cm.get("height", 0) or 0)
    return (l * w * h) / VOLUMETRIC_DIVISOR_CM3_PER_KG


def chargeable_weight_kg(weight_kg: float, dimensions_cm: Optional[Dict], quantity: int = 1) -> float:
    actual = float(weight_kg or 0)
    volumetric = volumetric_weight_kg(dimensions_cm) * max(1, quantity)
    return max(actual, volumetric)


def road_distance_km(origin: Optional[Dict], destination: Optional[Dict], override: Optional[float] = None) -> float:
    if override is not None:
        return float(override)
    if not origin or not destination:
        return 0.0
    straight = haversine_km(origin["lat"], origin["lon"], destination["lat"], destination["lon"])
    return straight * ROAD_DISTANCE_FACTOR


def estimate_rate(
    *,
    weight_kg: float,
    dimensions_cm: Optional[Dict] = None,
    quantity: int = 1,
    load_type: str = "general",
    classifications: Sequence[str] = (),
    origin: Optional[Dict] = None,
    destination: Optional[Dict] = None,
    distance_km: Optional[float] = None,
    urgency_factor: float = 1.0,
    currency: str = "USD",
) -> RateEstimate:
    distance = road_distance_km(origin, destination, distance_km)
    cw = chargeable_weight_kg(weight_kg, dimensions_cm, quantity)
    tonnes = cw / 1000.0
    load_mult = LOAD_TYPE_MULTIPLIER.get(load_type, 1.0)
    surcharge = sum(CLASSIFICATION_SURCHARGE.get(c, 0.0) for c in classifications)
    urgency = max(1.0, float(urgency_factor))

    raw = distance * tonnes * BASE_RATE_PER_TONNE_KM * load_mult * (1 + surcharge) * urgency
    recommended = max(MIN_CHARGE, raw)
    return RateEstimate(
        distance_km=distance,
        chargeable_weight_kg=cw,
        load_multiplier=load_mult,
        surcharge_fraction=surcharge,
        urgency_factor=urgency,
        recommended=recommended,
        low=recommended * (1 - BAND_SPREAD),
        high=recommended * (1 + BAND_SPREAD),
        currency=currency,
        breakdown={
            "base_rate_per_tonne_km": BASE_RATE_PER_TONNE_KM,
            "tonnes": round(tonnes, 4),
            "applied_surcharges": [c for c in classifications if c in CLASSIFICATION_SURCHARGE],
            "min_charge": MIN_CHARGE,
        },
    )
