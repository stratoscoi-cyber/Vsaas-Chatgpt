"""Client for the TRAAS (prisaTravel) routing service.

Used by prisaMove reroutes: when a shipment hits a hazard (flooding, security,
accident…), LGaaS asks TRAAS for a hazard-avoiding route and feeds the returned
distance back into the ETA. The client is a thin HTTP adapter so it can be faked
in tests; failures are surfaced as :class:`RoutingClientError` and the caller
falls back to the straight-line estimate.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import requests

# Map a reroute reason to a TRAAS hazard class so the avoidance is meaningful.
REASON_TO_HAZARD = {
    "reroute_flooding": "flood",
    "reroute_security": "safety",
    "reroute_accident": "traffic",
    "reroute_weather": "weather",
    "reroute_road_closure": "red_zone",
    "reroute_bridge": "red_zone",
}


class RoutingClientError(RuntimeError):
    pass


class TraasClient:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def add_hazard(self, hazard_type: str, location: Dict, severity: str = "High", radius_km: float = 3.0) -> Optional[str]:
        """Register a hazard with TRAAS so optimize routes around it (best effort)."""
        try:
            r = requests.post(
                f"{self.base_url}/api/hazards",
                json={"hazard_type": hazard_type, "severity": severity,
                      "location": location, "radius_km": radius_km},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json().get("hazard_id")
        except (requests.RequestException, ValueError):
            return None  # non-fatal; optimize still avoids existing active hazards

    def optimize(self, origin: List[float], destination: List[float],
                 vehicle_type: str = "agricultural_vehicle", hazards: Optional[list] = None) -> Dict:
        """Return ``{distance_km, duration_min, path}`` for a hazard-aware route."""
        try:
            r = requests.post(
                f"{self.base_url}/api/routes/optimize",
                json={"origin": origin, "destination": destination,
                      "vehicle_type": vehicle_type, **({"hazards": hazards} if hazards is not None else {})},
                timeout=self.timeout,
            )
            r.raise_for_status()
            route = r.json().get("route", {})
        except requests.RequestException as exc:
            raise RoutingClientError(f"traas optimize failed: {exc}") from exc
        except ValueError as exc:
            raise RoutingClientError(f"invalid traas response: {exc}") from exc
        return {
            "distance_km": route.get("distance_km"),
            "duration_min": route.get("duration_min"),
            "path": route.get("path"),
        }


def make_traas_client(traas_url: Optional[str]) -> Optional[TraasClient]:
    return TraasClient(traas_url) if traas_url else None
