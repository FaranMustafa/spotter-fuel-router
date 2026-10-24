"""Unit tests for fuel.services.corridor (pure numpy, no Django)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pytest

from fuel.services import corridor
from fuel.services.corridor import (
    EARTH_RADIUS_MILES,
    Candidate,
    find_candidates,
    resample_route,
    route_length_miles,
    route_mile_markers,
)

MILES_PER_DEGREE = EARTH_RADIUS_MILES * math.pi / 180.0  # ~69.09


@dataclass
class Stations:
    ids: np.ndarray
    lats: np.ndarray
    lons: np.ndarray
    prices: np.ndarray


def make_stations(rows: list[tuple[int, float, float, float]]) -> Stations:
    """rows: (id, lon, lat, price)."""
    return Stations(
        ids=np.array([r[0] for r in rows], dtype=np.int64),
        lons=np.array([r[1] for r in rows], dtype=np.float64),
        lats=np.array([r[2] for r in rows], dtype=np.float64),
        prices=np.array([r[3] for r in rows], dtype=np.float64),
    )


def haversine_miles(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, (lon1, lat1, lon2, lat2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


# --- mile markers -------------------------------------------------------------------


def test_one_degree_of_latitude_is_about_69_miles() -> None:
    markers = route_mile_markers([(-100.0, 40.0), (-100.0, 41.0), (-100.0, 42.0)])
    assert markers[0] == 0.0
    assert markers[1] == pytest.approx(69.09, abs=0.01)
    assert markers[2] == pytest.approx(2 * 69.09, abs=0.02)
    assert route_length_miles([(-100.0, 40.0), (-100.0, 42.0)]) == pytest.approx(markers[2])


def test_markers_single_vertex_and_duplicates() -> None:
    assert route_mile_markers([(-90.0, 35.0)]).tolist() == [0.0]
    markers = route_mile_markers([(-90.0, 35.0), (-90.0, 35.0), (-90.0, 36.0)])
    assert markers[1] == 0.0
    assert markers[2] == pytest.approx(69.09, abs=0.01)


def test_markers_reject_bad_shape() -> None:
    with pytest.raises(ValueError):
        route_mile_markers([])
    with pytest.raises(ValueError):
        route_mile_markers([(1.0, 2.0, 3.0)])


# --- resampling ---------------------------------------------------------------------


@pytest.mark.parametrize("spacing", [1.0, 0.25, 7.5])
def test_resample_spacing_and_endpoints(spacing: float) -> None:
    coords = [(-100.0, 40.0), (-100.0, 41.0), (-99.9, 41.0), (-98.0, 39.5)]
    markers = route_mile_markers(coords)
    samples, sample_markers = resample_route(coords, markers, spacing)

    assert samples.shape == (sample_markers.size, 2)
    np.testing.assert_allclose(samples[0], coords[0])
    np.testing.assert_allclose(samples[-1], coords[-1])
    assert sample_markers[0] == 0.0
    assert sample_markers[-1] == pytest.approx(markers[-1])
    gaps = np.diff(sample_markers)
    assert gaps.min() > 0
    assert gaps.max() <= spacing + 1e-9
    # Every original vertex is a sample.
    for vertex, mark in zip(coords, markers, strict=True):
        hits = np.nonzero(np.all(np.isclose(samples, vertex), axis=1))[0]
        assert hits.size >= 1
        assert sample_markers[hits[0]] == pytest.approx(mark)


def test_resample_interpolates_linearly() -> None:
    coords = [(-100.0, 40.0), (-100.0, 41.0)]
    samples, sample_markers = resample_route(coords, route_mile_markers(coords), 1.0)
    assert samples.shape[0] == 71  # 69.09 miles -> 70 pieces
    # Latitude advances in proportion to the mile marker on a meridian.
    np.testing.assert_allclose(samples[:, 1] - 40.0, sample_markers / sample_markers[-1])
    np.testing.assert_allclose(samples[:, 0], -100.0)


def test_resample_rejects_bad_input() -> None:
    coords = [(-100.0, 40.0), (-100.0, 41.0)]
    with pytest.raises(ValueError):
        resample_route(coords, route_mile_markers(coords), 0.0)
    with pytest.raises(ValueError):
        resample_route(coords, np.array([0.0]), 1.0)


# --- candidates ---------------------------------------------------------------------

# East-west route along latitude 40 from lon -100 to -90 (~530 miles), vertices every 1 deg.
EW_LAT = 40.0
EW_ROUTE = [(-100.0 + i, EW_LAT) for i in range(11)]
MILES_PER_LON_DEG = MILES_PER_DEGREE * math.cos(math.radians(EW_LAT))


def offset_lat(miles_north: float) -> float:
    return EW_LAT + miles_north / MILES_PER_DEGREE


def test_synthetic_east_west_route_positions_and_detours() -> None:
    stations = make_stations(
        [
            (1, -99.5, offset_lat(0.0), 3.0),  # on the route
            (2, -97.25, offset_lat(3.0), 3.5),  # 3 mi north
            (3, -95.0, offset_lat(-7.0), 4.0),  # 7 mi south
            (4, -92.1, offset_lat(9.9), 3.1),  # just inside the corridor
            (5, -94.0, offset_lat(10.5), 2.0),  # just outside
            (6, -93.0, offset_lat(50.0), 2.0),  # far outside
            (7, -88.0, EW_LAT, 2.0),  # beyond the east end (~106 mi)
            (8, -100.05, EW_LAT, 3.3),  # 2.65 mi before the start
        ]
    )
    candidates = find_candidates(EW_ROUTE, stations, corridor_miles=10.0)
    by_id = {c.station_id: c for c in candidates}

    assert set(by_id) == {1, 2, 3, 4, 8}
    expected = {
        1: (0.5 * MILES_PER_LON_DEG, 0.0),
        2: (2.75 * MILES_PER_LON_DEG, 3.0),
        3: (5.0 * MILES_PER_LON_DEG, 7.0),
        4: (7.9 * MILES_PER_LON_DEG, 9.9),
        8: (0.0, 0.05 * MILES_PER_LON_DEG),
    }
    for station_id, (position, detour) in expected.items():
        c = by_id[station_id]
        assert c.position_miles == pytest.approx(position, abs=1.0)
        assert c.detour_miles == pytest.approx(detour, abs=0.1)
    assert [c.position_miles for c in candidates] == sorted(c.position_miles for c in candidates)
    assert all(isinstance(c.station_id, int) and isinstance(c.price, float) for c in candidates)


def test_candidates_sorted_by_position_then_price() -> None:
    stations = make_stations(
        [
            (10, -96.0, offset_lat(2.0), 4.0),
            (11, -96.0, offset_lat(2.0), 3.0),
            (12, -98.0, offset_lat(-1.0), 5.0),
        ]
    )
    candidates = find_candidates(EW_ROUTE, stations, corridor_miles=10.0)
    assert [c.station_id for c in candidates] == [12, 11, 10]
    assert candidates == sorted(candidates)
    assert Candidate(1.0, 3.0, 1, 0.0) < Candidate(1.0, 3.5, 0, 0.0) < Candidate(2.0, 1.0, 0, 0.0)


def test_no_stations_and_none_in_corridor() -> None:
    empty = make_stations([])
    assert find_candidates(EW_ROUTE, empty, corridor_miles=10.0) == []
    far = make_stations([(1, -80.0, 30.0, 3.0)])
    assert find_candidates(EW_ROUTE, far, corridor_miles=10.0) == []


def test_accepts_precomputed_markers() -> None:
    stations = make_stations([(1, -95.0, offset_lat(1.0), 3.0)])
    markers = route_mile_markers(EW_ROUTE)
    a = find_candidates(EW_ROUTE, stations, 10.0, markers=markers)
    b = find_candidates(EW_ROUTE, stations, 10.0)
    assert a == b


def test_matches_brute_force_on_wiggly_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Many stations (several chunks, small blocks) against a dense haversine brute force."""
    monkeypatch.setattr(corridor, "_MAX_MATRIX_ELEMENTS", 20_000)
    rng = np.random.default_rng(42)
    t = np.linspace(0.0, 1.0, 400)
    route = np.column_stack([-104.0 + 12.0 * t, 37.0 + 2.0 * np.sin(6.0 * t) + 3.0 * t])
    n = 1_800
    k = rng.integers(0, t.size, n)
    stations = Stations(
        ids=np.arange(n, dtype=np.int64),
        lons=route[k, 0] + rng.uniform(-0.4, 0.4, n),
        lats=route[k, 1] + rng.uniform(-0.4, 0.4, n),
        prices=rng.uniform(2.5, 6.5, n),
    )
    corridor_miles = 10.0
    candidates = find_candidates(route, stations, corridor_miles)
    got = {c.station_id: c for c in candidates}

    dense, dense_markers = resample_route(route, route_mile_markers(route), 0.05)
    for i in range(n):
        d = haversine_miles(stations.lons[i], stations.lats[i], dense[:, 0], dense[:, 1])
        j = int(np.argmin(d))
        true_detour = float(d[j])
        if abs(true_detour - corridor_miles) < 0.1:
            continue  # borderline: either answer is acceptable
        if true_detour < corridor_miles:
            assert i in got, f"station {i} at {true_detour:.2f} mi missing"
            assert got[i].detour_miles == pytest.approx(true_detour, abs=0.1)
            assert got[i].position_miles == pytest.approx(dense_markers[j], abs=1.0)
        else:
            assert i not in got
