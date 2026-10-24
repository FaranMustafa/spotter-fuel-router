"""Polyline simplification for responses (Douglas-Peucker). Pure numpy, no Django.

The planner and corridor search always use the full OSRM geometry; only the geometry
returned to clients is simplified, to keep responses small.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

# About 50 m of latitude: invisible at the zoom levels a cross-country route is viewed at.
DEFAULT_TOLERANCE_DEG = 0.0005


def simplify[T: Sequence[float]](coordinates: Sequence[T], tolerance_deg: float) -> list[T]:
    """Douglas-Peucker simplification of a ``(lon, lat)`` polyline in degrees.

    Keeps the first and last points and every point needed so that no dropped point lies
    further than ``tolerance_deg`` (planar distance in degrees) from the simplified line.
    Iterative (an explicit stack of index ranges), so very long routes cannot hit the
    recursion limit. Returns the kept input items, in order.
    """
    if tolerance_deg < 0:
        raise ValueError("tolerance_deg must not be negative")
    n = len(coordinates)
    if n < 3:
        return list(coordinates)
    points = np.asarray(coordinates, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2:
        raise ValueError("coordinates must be a sequence of (lon, lat) pairs")

    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        start, end = points[first], points[last]
        inner = points[first + 1 : last]
        segment = end - start
        length_sq = float(segment @ segment)
        if length_sq == 0.0:  # closed loop: distance to the shared end point
            distances = np.hypot(*(inner - start).T)
        else:
            # Distance to the segment (not the infinite line), so points beyond either
            # end of the chord are measured to that end.
            t = np.clip(((inner - start) @ segment) / length_sq, 0.0, 1.0)
            nearest = start + t[:, None] * segment
            distances = np.hypot(*(inner - nearest).T)
        index = int(np.argmax(distances))
        if distances[index] > tolerance_deg:
            split = first + 1 + index
            keep[split] = True
            stack.append((first, split))
            stack.append((split, last))
    return [coordinates[i] for i in np.flatnonzero(keep)]
