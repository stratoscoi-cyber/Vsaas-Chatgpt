"""Unit tests for the pure geospatial / agronomy helpers."""

import math

from agri_platform.common import geo


def test_haversine_known_distance():
    # London -> Paris is ~343 km.
    d = geo.haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
    assert 330 < d < 360


def test_haversine_zero():
    assert geo.haversine_km(10, 10, 10, 10) == 0


def test_path_length_sums_segments():
    coords = [(0.0, 0.0), (0.0, 1.0), (0.0, 2.0)]
    one_deg = geo.haversine_km(0, 0, 0, 1)
    assert math.isclose(geo.path_length_km(coords), 2 * one_deg, rel_tol=1e-6)


def test_buffer_point_ring_is_closed_and_lonlat():
    ring = geo.buffer_point_ring(10.0, 7.0, 5.0, segments=8)
    assert ring[0] == ring[-1]  # closed
    assert len(ring) == 9
    # ring is [lon, lat]; first point sits to the east at the same latitude.
    assert ring[0][0] > 7.0
    assert math.isclose(ring[0][1], 10.0, abs_tol=1e-9)


def test_buffer_path_bbox_grows_box():
    coords = [(10.0, 7.0), (11.0, 8.0)]
    min_lon, min_lat, max_lon, max_lat = geo.buffer_path_bbox(coords, 10.0)
    assert min_lon < 7.0 and min_lat < 10.0
    assert max_lon > 8.0 and max_lat > 11.0


def test_points_to_avoid_rings_one_per_point():
    rings = geo.points_to_avoid_rings([(10.0, 7.0), (11.0, 8.0)], buffer_km=1.0)
    assert len(rings) == 2
    assert all(r[0] == r[-1] for r in rings)


def test_grid_points_within_bbox():
    pts = geo.grid_points((0.0, 0.0, 0.2, 0.2), step_deg=0.1)
    assert (0.0, 0.0) in pts
    assert all(0 <= lat <= 0.2 + 1e-9 and 0 <= lon <= 0.2 + 1e-9 for lat, lon in pts)


def test_calculate_gdd_basic():
    res = geo.calculate_gdd([20, 30], [10, 10], base_temp=10)
    # day1: (20+10)/2-10 = 5 ; day2: (30+10)/2-10 = 10
    assert res["daily"] == [5.0, 10.0]
    assert res["total"] == 15.0
    assert res["days"] == 2


def test_calculate_gdd_never_negative():
    res = geo.calculate_gdd([5], [0], base_temp=10)
    assert res["daily"] == [0.0]


def test_calculate_gdd_upper_cap():
    res = geo.calculate_gdd([40], [30], base_temp=10, upper_temp=30)
    # both capped to 30 -> (30+30)/2 - 10 = 20
    assert res["daily"] == [20.0]


def test_decode_polyline_known_precision5():
    # The canonical Google example, precision 5:
    # (38.5, -120.2), (40.7, -120.95), (43.252, -126.453)
    encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    pts = geo.decode_polyline(encoded, precision=5)
    assert len(pts) == 3
    assert math.isclose(pts[0][0], 38.5, abs_tol=1e-4)
    assert math.isclose(pts[0][1], -120.2, abs_tol=1e-4)
    assert math.isclose(pts[2][0], 43.252, abs_tol=1e-4)
    assert math.isclose(pts[2][1], -126.453, abs_tol=1e-4)


def test_decode_polyline_empty():
    assert geo.decode_polyline("") == []
