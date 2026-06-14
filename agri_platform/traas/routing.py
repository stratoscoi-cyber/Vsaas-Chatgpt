"""Routing engine adapters.

The service depends on the small :class:`RoutingEngine` protocol, so the actual
engine (Valhalla over HTTP, or an offline straight-line stub for tests/demos)
can be swapped freely. Coordinates are ``(lat, lon)`` everywhere at this
boundary; the Valhalla adapter handles the lon/lat flip required by GeoJSON
avoid-polygons internally.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, Sequence, Tuple

import requests

from ..common.geo import decode_polyline, haversine_km

Coord = Tuple[float, float]
Ring = List[List[float]]  # list of [lon, lat]


class RoutingError(RuntimeError):
    """Raised when a route cannot be computed."""


class RoutingEngine(Protocol):
    def route(
        self,
        origin: Coord,
        destination: Coord,
        avoid_rings: Sequence[Ring] = (),
        costing: str = "auto",
    ) -> Dict: ...


class ValhallaEngine:
    """Adapter for a Valhalla ``/route`` endpoint (local Docker or hosted)."""

    def __init__(self, url: str = "http://localhost:8002/route", timeout: float = 15.0):
        self.url = url
        self.timeout = timeout

    def route(self, origin, destination, avoid_rings=(), costing="auto") -> Dict:
        payload = {
            "locations": [
                {"lat": origin[0], "lon": origin[1]},
                {"lat": destination[0], "lon": destination[1]},
            ],
            "costing": costing,
            "directions_options": {"units": "kilometers"},
        }
        if avoid_rings:
            payload["exclude_polygons"] = list(avoid_rings)
        try:
            resp = requests.post(self.url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise RoutingError(f"valhalla request failed: {exc}") from exc
        except ValueError as exc:
            raise RoutingError(f"invalid valhalla response: {exc}") from exc

        trip = data.get("trip", {})
        legs = trip.get("legs", [])
        coords: List[Coord] = []
        for leg in legs:
            coords.extend(decode_polyline(leg.get("shape", ""), precision=6))
        summary = trip.get("summary", {})
        return {
            "coordinates": coords,
            "distance_km": summary.get("length", 0.0),
            "duration_min": summary.get("time", 0.0) / 60.0,
            "engine": "valhalla",
        }


class StraightLineEngine:
    """Offline fallback that returns the geodesic segment between the points.

    Useful for tests and for demos where no Valhalla instance is available. It
    ignores ``avoid_rings`` beyond reporting them, so it is not a real detour
    planner -- it only makes the rest of the stack runnable end-to-end.
    """

    AVG_SPEED_KMH = 50.0

    def route(self, origin, destination, avoid_rings=(), costing="auto") -> Dict:
        distance = haversine_km(origin[0], origin[1], destination[0], destination[1])
        return {
            "coordinates": [list(origin), list(destination)],
            "distance_km": round(distance, 3),
            "duration_min": round(distance / self.AVG_SPEED_KMH * 60.0, 1),
            "engine": "straight_line",
            "avoided": len(avoid_rings),
        }
