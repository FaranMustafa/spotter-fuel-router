"""Find fuel stations near a driving route and place them on the route's mile scale.

Pure numpy, no Django and no I/O, so it is unit-testable in isolation.

Coordinates are ``(lon, lat)`` pairs in degrees, as in GeoJSON. Distances are in miles.

Method:

1. Cumulative mile markers per route vertex with a vectorised haversine.
2. Resample the route so consecutive samples are at most ``spacing_miles`` apart
   (linear interpolation along each segment; first and last vertex always kept).
3. Prefilter stations with the route bounding box expanded by ``corridor_miles``.
4. For every remaining station find the nearest sample with an equirectangular
   approximation (``dx = dlon * cos(lat_sample)``, ``dy = dlat``), in chunks so the
   temporary distance matrix stays small (well under 100 MB).
5. Refine each hit by projecting the station onto the two route segments adjacent to its
   nearest sample. This makes ``detour_miles`` the perpendicular distance to the route
   (not the distance to the nearest sample, which overestimates by up to half the
   sample spacing) and interpolates ``position_miles`` between the sample markers.
6. Keep stations whose refined distance is ``<= corridor_miles``; sort by position,
   then price.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
MILES_PER_DEGREE = EARTH_RADIUS_MILES * math.pi / 180.0

# Stations per chunk and the cap on elements of one temporary (stations x samples)
# float64 matrix: 1.5M elements = 12 MB, and at most ~4 such temporaries are alive at
# once, so peak extra memory stays around 50 MB regardless of route length.
_STATION_CHUNK = 500
_MAX_MATRIX_ELEMENTS = 1_500_000


class StationIndexLike(Protocol):
    """Parallel arrays describing every station (one entry per station)."""

    ids: np.ndarray  # int64
    lats: np.ndarray  # float64, degrees
    lons: np.ndarray  # float64, degrees
    prices: np.ndarray  # float64, USD per gallon


@dataclass(frozen=True, order=True)
class Candidate:
    """A station inside the corridor, located on the route's mile scale.

    Field order defines the natural sort order: position, then price (then id, detour).
    """

    position_miles: float
    price: float
    station_id: int
    detour_miles: float


def _as_lonlat(coordinates: Sequence[tuple[float, float]] | np.ndarray) -> np.ndarray:
    coords = np.asarray(coordinates, dtype=np.float64)
    if coords.ndim != 2 or coords.shape[1] != 2 or coords.shape[0] == 0:
        raise ValueError("coordinates must be a non-empty sequence of (lon, lat) pairs")
    return coords


def route_mile_markers(coordinates: Sequence[tuple[float, float]] | np.ndarray) -> np.ndarray:
    """Cumulative distance in miles at every vertex of the route (first entry is 0)."""
    coords = _as_lonlat(coordinates)
    lon = np.radians(coords[:, 0])
    lat = np.radians(coords[:, 1])
    dlat = np.diff(lat)
    dlon = np.diff(lon)
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2.0) ** 2
    segment_miles = 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    markers = np.empty(coords.shape[0], dtype=np.float64)
    markers[0] = 0.0
    np.cumsum(segment_miles, out=markers[1:])
    return markers


def route_length_miles(coordinates: Sequence[tuple[float, float]] | np.ndarray) -> float:
    """Total route length in miles (the last mile marker)."""
    return float(route_mile_markers(coordinates)[-1])


def resample_route(
    coordinates: Sequence[tuple[float, float]] | np.ndarray,
    markers: np.ndarray,
    spacing_miles: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate extra points so consecutive samples are at most ``spacing_miles`` apart.

    Returns ``(sample_lonlat[N, 2], sample_markers[N])``. Every original vertex is kept
    (so the first and last vertex always are); segments longer than the spacing are split
    into equal pieces.
    """
    if spacing_miles <= 0:
        raise ValueError("spacing_miles must be positive")
    coords = _as_lonlat(coordinates)
    markers = np.asarray(markers, dtype=np.float64)
    if markers.shape != (coords.shape[0],):
        raise ValueError("markers must have one entry per coordinate")
    if coords.shape[0] == 1:
        return coords.copy(), markers.copy()

    segment_miles = np.diff(markers)
    pieces = np.maximum(1, np.ceil(segment_miles / spacing_miles - 1e-12)).astype(np.int64)
    segment_index = np.repeat(np.arange(pieces.size), pieces)
    starts = np.cumsum(pieces) - pieces
    step_in_segment = np.arange(segment_index.size) - starts[segment_index]
    t = step_in_segment / pieces[segment_index]

    p0 = coords[segment_index]
    p1 = coords[segment_index + 1]
    sample_lonlat = np.empty((segment_index.size + 1, 2), dtype=np.float64)
    sample_lonlat[:-1] = p0 + (p1 - p0) * t[:, None]
    sample_lonlat[-1] = coords[-1]

    sample_markers = np.empty(segment_index.size + 1, dtype=np.float64)
    sample_markers[:-1] = markers[segment_index] + segment_miles[segment_index] * t
    sample_markers[-1] = markers[-1]
    return sample_lonlat, sample_markers


def _nearest_samples(
    st_lon: np.ndarray,
    st_lat: np.ndarray,
    s_lon: np.ndarray,
    s_lat: np.ndarray,
    s_cos: np.ndarray,
    window_lon_degrees: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Nearest sample index and its distance (miles) for each station, chunked."""
    n = st_lon.size
    nearest = np.zeros(n, dtype=np.int64)
    distance = np.full(n, np.inf, dtype=np.float64)
    if n == 0:
        return nearest, distance

    # Stations sorted by longitude, so each chunk only needs the samples inside its
    # own longitude window: on an east-west route that cuts the work per chunk sharply.
    order = np.argsort(st_lon, kind="stable")
    for start in range(0, n, _STATION_CHUNK):
        rows = order[start : start + _STATION_CHUNK]
        lo = st_lon[rows].min() - window_lon_degrees
        hi = st_lon[rows].max() + window_lon_degrees
        cols = np.nonzero((s_lon >= lo) & (s_lon <= hi))[0]
        if cols.size == 0:
            continue
        sub_lon, sub_lat, sub_cos = s_lon[cols], s_lat[cols], s_cos[cols]
        block = max(1, _MAX_MATRIX_ELEMENTS // cols.size)
        for b in range(0, rows.size, block):
            r = rows[b : b + block]
            dx = (st_lon[r, None] - sub_lon[None, :]) * sub_cos[None, :]
            dy = st_lat[r, None] - sub_lat[None, :]
            d2 = dx * dx
            d2 += dy * dy
            best = np.argmin(d2, axis=1)
            nearest[r] = cols[best]
            distance[r] = np.sqrt(d2[np.arange(r.size), best]) * MILES_PER_DEGREE
    return nearest, distance


def _refine(
    st_lon: np.ndarray,
    st_lat: np.ndarray,
    nearest: np.ndarray,
    s_lon: np.ndarray,
    s_lat: np.ndarray,
    s_markers: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Project each station onto the segments either side of its nearest sample.

    Returns ``(position_miles, detour_miles)``. Works in a local equirectangular frame
    centred on each station, which is accurate to well under 0.1% at corridor scale.
    """
    last = s_lon.size - 1
    cos_st = np.cos(np.radians(st_lat))

    def project(i0: np.ndarray, i1: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        px = (s_lon[i0] - st_lon) * cos_st * MILES_PER_DEGREE
        py = (s_lat[i0] - st_lat) * MILES_PER_DEGREE
        qx = (s_lon[i1] - st_lon) * cos_st * MILES_PER_DEGREE
        qy = (s_lat[i1] - st_lat) * MILES_PER_DEGREE
        vx, vy = qx - px, qy - py
        length2 = vx * vx + vy * vy
        safe = np.where(length2 > 0.0, length2, 1.0)
        t = np.where(length2 > 0.0, np.clip(-(px * vx + py * vy) / safe, 0.0, 1.0), 0.0)
        cx, cy = px + t * vx, py + t * vy
        dist = np.hypot(cx, cy)
        pos = s_markers[i0] + t * (s_markers[i1] - s_markers[i0])
        return pos, dist

    prev_pos, prev_dist = project(np.maximum(nearest - 1, 0), nearest)
    next_pos, next_dist = project(nearest, np.minimum(nearest + 1, last))
    use_prev = prev_dist <= next_dist
    return np.where(use_prev, prev_pos, next_pos), np.where(use_prev, prev_dist, next_dist)


def find_candidates(
    coordinates: Sequence[tuple[float, float]] | np.ndarray,
    stations: StationIndexLike,
    corridor_miles: float,
    spacing_miles: float = 1.0,
    *,
    markers: np.ndarray | None = None,
) -> list[Candidate]:
    """Stations within ``corridor_miles`` of the route, sorted by position then price.

    ``markers`` may be passed when the caller already computed
    :func:`route_mile_markers`; the total route length is ``markers[-1]``
    (or :func:`route_length_miles`).
    """
    if corridor_miles < 0:
        raise ValueError("corridor_miles must be non-negative")
    coords = _as_lonlat(coordinates)
    if markers is None:
        markers = route_mile_markers(coords)
    samples, s_markers = resample_route(coords, markers, spacing_miles)
    s_lon = np.ascontiguousarray(samples[:, 0])
    s_lat = np.ascontiguousarray(samples[:, 1])
    s_cos = np.cos(np.radians(s_lat))

    ids = np.asarray(stations.ids, dtype=np.int64)
    lats = np.asarray(stations.lats, dtype=np.float64)
    lons = np.asarray(stations.lons, dtype=np.float64)
    prices = np.asarray(stations.prices, dtype=np.float64)
    if not (ids.shape == lats.shape == lons.shape == prices.shape) or ids.ndim != 1:
        raise ValueError("station arrays must be one-dimensional and the same length")
    if ids.size == 0:
        return []

    # A station whose perpendicular distance is <= corridor can be up to half a sample
    # spacing further from its nearest sample; keep those for the refinement step.
    coarse_limit = corridor_miles + spacing_miles / 2.0 + 1e-9

    # Bounding-box prefilter, expanded by the coarse limit. The longitude margin uses the
    # smallest cos(lat) in the box so it is never too narrow.
    lat_margin = coarse_limit / MILES_PER_DEGREE
    lat_min, lat_max = s_lat.min() - lat_margin, s_lat.max() + lat_margin
    min_cos = max(math.cos(math.radians(min(max(abs(lat_min), abs(lat_max)), 89.0))), 1e-6)
    lon_margin = coarse_limit / (MILES_PER_DEGREE * min_cos)
    in_box = (
        (lats >= lat_min)
        & (lats <= lat_max)
        & (lons >= s_lon.min() - lon_margin)
        & (lons <= s_lon.max() + lon_margin)
    )
    idx = np.nonzero(in_box)[0]
    if idx.size == 0:
        return []

    nearest, coarse = _nearest_samples(lons[idx], lats[idx], s_lon, s_lat, s_cos, lon_margin)
    near = coarse <= coarse_limit
    idx, nearest = idx[near], nearest[near]
    if idx.size == 0:
        return []

    position, detour = _refine(lons[idx], lats[idx], nearest, s_lon, s_lat, s_markers)
    keep = detour <= corridor_miles
    candidates = [
        Candidate(
            position_miles=float(p),
            price=float(pr),
            station_id=int(i),
            detour_miles=float(d),
        )
        for p, pr, i, d in zip(
            position[keep], prices[idx[keep]], ids[idx[keep]], detour[keep], strict=True
        )
    ]
    candidates.sort()
    return candidates
