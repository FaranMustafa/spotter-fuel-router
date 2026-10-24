"""Unit tests for fuel.services.geometry (pure numpy, no Django)."""

from __future__ import annotations

import math
import random

import pytest

from fuel.services.geometry import DEFAULT_TOLERANCE_DEG, simplify


def test_straight_line_collapses_to_its_ends() -> None:
    line = [(-110.0 + i * 0.01, 35.0 + i * 0.005) for i in range(1000)]
    assert simplify(line, DEFAULT_TOLERANCE_DEG) == [line[0], line[-1]]


def test_zigzag_keeps_every_corner() -> None:
    # Corners every 1 degree, with 9 collinear points on each leg between them.
    corners = [(float(i), float(i % 2)) for i in range(11)]
    line = []
    for (x0, y0), (x1, y1) in zip(corners, corners[1:], strict=False):
        line.extend((x0 + (x1 - x0) * k / 10, y0 + (y1 - y0) * k / 10) for k in range(10))
    line.append(corners[-1])
    assert len(line) == 101
    assert simplify(line, DEFAULT_TOLERANCE_DEG) == corners


def test_points_within_tolerance_are_dropped_and_beyond_are_kept() -> None:
    line = [(0.0, 0.0), (1.0, 0.0004), (2.0, 0.0), (3.0, 0.0006), (4.0, 0.0)]
    assert simplify(line, 0.0005) == [(0.0, 0.0), (3.0, 0.0006), (4.0, 0.0)]


def test_short_inputs_and_closed_loops() -> None:
    assert simplify([], 0.1) == []
    assert simplify([(1.0, 2.0)], 0.1) == [(1.0, 2.0)]
    assert simplify([(1.0, 2.0), (3.0, 4.0)], 0.1) == [(1.0, 2.0), (3.0, 4.0)]
    loop = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
    assert simplify(loop, 0.01) == loop


def test_returns_the_input_items_and_validates() -> None:
    line = [[0.0, 0.0], [0.5, 0.0], [1.0, 1.0]]
    result = simplify(line, 0.01)
    assert result == line and all(a is b for a, b in zip(result, line, strict=True))
    with pytest.raises(ValueError):
        simplify(line, -1)
    with pytest.raises(ValueError):
        simplify([(0.0, 0.0, 0.0)] * 3, 0.1)


def test_every_dropped_point_is_within_tolerance() -> None:
    rng = random.Random(3)
    line = [(i * 0.001, rng.uniform(-0.002, 0.002)) for i in range(5000)]
    kept = simplify(line, DEFAULT_TOLERANCE_DEG)
    assert kept[0] == line[0] and kept[-1] == line[-1]
    index = {p: i for i, p in enumerate(line)}
    positions = [index[p] for p in kept]
    assert positions == sorted(positions)
    for a, b in zip(positions, positions[1:], strict=False):
        (x0, y0), (x1, y1) = line[a], line[b]
        dx, dy = x1 - x0, y1 - y0
        for x, y in line[a + 1 : b]:
            t = min(1.0, max(0.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
            distance = math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))
            assert distance <= DEFAULT_TOLERANCE_DEG + 1e-12
