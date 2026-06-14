"""Pure-Python geospatial helpers.

These functions intentionally avoid heavy GIS dependencies (geopandas / shapely
/ pyproj) so the domain logic stays importable and unit-testable everywhere,
including minimal environments and CI. The approximations used (equirectangular
distance, latitude-scaled degree buffers) are accurate enough for the corridor
buffering, hazard zones and grid sampling this platform needs. When centimetre
accuracy is required, swap these for projected (UTM) shapely operations.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE_LAT = 111.32

Coord = Tuple[float, float]  # (lat, lon)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometres."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def path_length_km(coords: Sequence[Coord]) -> float:
    """Total length of a polyline given as a sequence of ``(lat, lon)`` points."""
    return sum(
        haversine_km(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        for i in range(len(coords) - 1)
    )


def _deg_per_km_lon(lat: float) -> float:
    """Degrees of longitude per kilometre at a given latitude."""
    scale = max(0.01, math.cos(math.radians(lat)))
    return 1.0 / (KM_PER_DEGREE_LAT * scale)


def buffer_point_ring(lat: float, lon: float, radius_km: float, segments: int = 24) -> List[List[float]]:
    """Return a closed ``[lon, lat]`` ring approximating a circle around a point.

    The ring is GeoJSON-ordered (longitude first) so it can be dropped straight
    into a ``Polygon``/``MultiPolygon`` geometry or a Valhalla avoid-polygon.
    """
    dlat = radius_km / KM_PER_DEGREE_LAT
    dlon = radius_km * _deg_per_km_lon(lat)
    ring = []
    for i in range(segments):
        theta = 2 * math.pi * i / segments
        ring.append([lon + dlon * math.cos(theta), lat + dlat * math.sin(theta)])
    ring.append(ring[0])  # close the ring
    return ring


def bbox_of(coords: Iterable[Coord]) -> Tuple[float, float, float, float]:
    """Bounding box ``(min_lon, min_lat, max_lon, max_lat)`` for ``(lat, lon)`` points."""
    coords = list(coords)
    if not coords:
        raise ValueError("bbox_of requires at least one coordinate")
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def buffer_path_bbox(coords: Sequence[Coord], buffer_km: float) -> Tuple[float, float, float, float]:
    """Bounding box of a route corridor: the path's bbox grown by ``buffer_km``."""
    min_lon, min_lat, max_lon, max_lat = bbox_of(coords)
    mid_lat = (min_lat + max_lat) / 2
    dlat = buffer_km / KM_PER_DEGREE_LAT
    dlon = buffer_km * _deg_per_km_lon(mid_lat)
    return (min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat)


def points_to_avoid_rings(points: Iterable[Coord], buffer_km: float = 2.0) -> List[List[List[float]]]:
    """Convert hazard points to buffered avoidance rings for a routing engine.

    Each input ``(lat, lon)`` becomes a circular ``[lon, lat]`` ring. The result
    matches Valhalla's ``avoid_polygons``/``exclude_polygons`` shape: a list of
    rings.
    """
    return [buffer_point_ring(lat, lon, buffer_km) for lat, lon in points]


def grid_points(
    bbox: Tuple[float, float, float, float], step_deg: float = 0.1
) -> List[Coord]:
    """Regular ``(lat, lon)`` sample grid inside ``(min_lon, min_lat, max_lon, max_lat)``."""
    min_lon, min_lat, max_lon, max_lat = bbox
    if step_deg <= 0:
        raise ValueError("step_deg must be positive")
    points: List[Coord] = []
    lat = min_lat
    while lat <= max_lat + 1e-9:
        lon = min_lon
        while lon <= max_lon + 1e-9:
            points.append((round(lat, 6), round(lon, 6)))
            lon += step_deg
        lat += step_deg
    return points


def calculate_gdd(
    tmax: Sequence[float],
    tmin: Sequence[float],
    base_temp: float = 10.0,
    upper_temp: float | None = None,
) -> dict:
    """Growing Degree Days from daily max/min temperatures.

    ``GDD_day = max(0, (Tmax + Tmin) / 2 - base_temp)``. When ``upper_temp`` is
    given, both temperatures are capped at it first (standard agronomic upper
    threshold). Lists of unequal length are truncated to the shorter one.
    """
    n = min(len(tmax), len(tmin))
    daily: List[float] = []
    for i in range(n):
        hi = float(tmax[i])
        lo = float(tmin[i])
        if upper_temp is not None:
            hi = min(hi, upper_temp)
            lo = min(lo, upper_temp)
        daily.append(max(0.0, (hi + lo) / 2.0 - base_temp))
    return {"daily": daily, "total": sum(daily), "days": n, "base_temp": base_temp}


def decode_polyline(encoded: str, precision: int = 6) -> List[Coord]:
    """Decode an encoded polyline (Valhalla uses precision 6) into ``(lat, lon)``."""
    if not encoded:
        return []
    coords: List[Coord] = []
    index = lat = lon = 0
    factor = float(10 ** precision)
    length = len(encoded)
    while index < length:
        for is_lon in (False, True):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / factor, lon / factor))
    return coords
