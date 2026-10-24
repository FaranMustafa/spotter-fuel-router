"""Unit tests for fuel.services.planner (pure Python, no Django)."""

from __future__ import annotations

import math
import random

import pytest

from fuel.services.corridor import Candidate
from fuel.services.planner import (
    Plan,
    PlannerConfig,
    UnreachableError,
    plan_stops,
)

FULL = PlannerConfig()  # 10 mpg, 500 miles, 50-gallon tank, full at start
TOL = 1e-6


def cand(position: float, price: float, station_id: int | None = None, detour: float = 0.0):
    sid = station_id if station_id is not None else int(position * 10)
    return Candidate(position_miles=position, price=price, station_id=sid, detour_miles=detour)


def replay(plan: Plan, candidates: list[Candidate], config: PlannerConfig) -> None:
    """Independently re-simulate the plan and check every invariant."""
    tank = config.tank_gallons
    by_id = {c.station_id: c for c in candidates}
    fuel = plan.initial_fuel_gallons
    previous = 0.0
    for n, stop in enumerate(plan.stops, start=1):
        c = by_id[stop.station_id]
        assert stop.order == n
        assert stop.position_miles == c.position_miles
        assert stop.position_miles > previous or (n == 1 and stop.position_miles >= 0)
        fuel -= (stop.position_miles - previous + 2 * c.detour_miles) / config.mpg
        assert fuel >= -TOL, "ran dry before a stop"
        assert stop.fuel_on_arrival_gallons == pytest.approx(fuel, abs=1e-6)
        assert stop.gallons_bought > 0
        assert stop.cost_usd == pytest.approx(stop.gallons_bought * c.price)
        fuel += stop.gallons_bought
        assert fuel <= tank + TOL, "overfilled the tank"
        assert stop.fuel_on_departure_gallons == pytest.approx(fuel, abs=1e-6)
        previous = stop.position_miles
    fuel -= (plan.route_miles - previous) / config.mpg
    assert fuel >= -TOL, "ran dry before the finish"
    assert plan.fuel_at_finish_gallons == pytest.approx(max(fuel, 0.0), abs=1e-6)
    assert plan.total_gallons == pytest.approx(sum(s.gallons_bought for s in plan.stops))
    assert plan.total_cost_usd == pytest.approx(sum(s.cost_usd for s in plan.stops))


# --- config -------------------------------------------------------------------------


def test_config_defaults_and_validation() -> None:
    assert FULL.tank_gallons == 50
    assert FULL.start_fuel_gallons == 50
    assert PlannerConfig(initial_fuel_gallons=12.5).start_fuel_gallons == 12.5
    with pytest.raises(ValueError):
        PlannerConfig(initial_fuel_gallons=51)
    with pytest.raises(ValueError):
        PlannerConfig(initial_fuel_gallons=-1)
    with pytest.raises(ValueError):
        PlannerConfig(mpg=0)


# --- scenarios ----------------------------------------------------------------------


def test_zero_stops_when_route_within_initial_range() -> None:
    plan = plan_stops([cand(100, 3.0), cand(200, 2.0)], 400, FULL)
    assert plan.stops == ()
    assert plan.total_gallons == 0
    assert plan.total_cost_usd == 0
    assert plan.fuel_at_finish_gallons == pytest.approx(10)
    assert plan.warnings and "no fuel stops" in plan.warnings[0]


def test_buys_just_enough_to_reach_cheaper_station() -> None:
    # Only A is reachable at the start; from A the finish is reachable, but B is cheaper,
    # so A sells just enough to reach B and B sells the rest.
    candidates = [cand(450, 4.0, 1), cand(600, 3.0, 2)]
    plan = plan_stops(candidates, 900, FULL)
    replay(plan, candidates, FULL)
    a, b = plan.stops
    assert (a.station_id, b.station_id) == (1, 2)
    assert a.fuel_on_arrival_gallons == pytest.approx(5)
    assert a.gallons_bought == pytest.approx(10)
    assert b.fuel_on_arrival_gallons == pytest.approx(0)
    assert b.gallons_bought == pytest.approx(30)
    assert plan.total_cost_usd == pytest.approx(10 * 4 + 30 * 3)
    assert plan.fuel_at_finish_gallons == pytest.approx(0)


def test_fills_up_when_all_ahead_are_pricier() -> None:
    candidates = [cand(100, 3.0, 1), cand(400, 4.0, 2), cand(700, 5.0, 3)]
    config = PlannerConfig(initial_fuel_gallons=10)
    plan = plan_stops(candidates, 1000, config)
    replay(plan, candidates, config)
    assert [s.station_id for s in plan.stops] == [1, 2, 3]
    assert [s.gallons_bought for s in plan.stops] == pytest.approx([50, 30, 10])
    assert [s.fuel_on_departure_gallons for s in plan.stops] == pytest.approx([50, 50, 30])
    assert plan.total_gallons == pytest.approx(90)
    assert plan.total_cost_usd == pytest.approx(150 + 120 + 50)


def test_fill_up_goes_to_cheapest_reachable_skipping_pricier() -> None:
    candidates = [cand(10, 3.0, 1), cand(200, 5.0, 2), cand(400, 4.0, 3), cand(450, 4.0, 4)]
    config = PlannerConfig(initial_fuel_gallons=1)
    plan = plan_stops(candidates, 800, config)
    replay(plan, candidates, config)
    # Station 2 is skipped; the 4.0 tie goes to the nearer station 3.
    assert [s.station_id for s in plan.stops] == [1, 3]


def test_first_stop_is_cheapest_reachable_on_initial_fuel() -> None:
    candidates = [cand(50, 4.0, 1), cand(300, 3.0, 2), cand(480, 3.5, 3)]
    plan = plan_stops(candidates, 900, FULL)
    replay(plan, candidates, FULL)
    assert plan.stops[0].station_id == 2
    assert plan.stops[0].fuel_on_arrival_gallons == pytest.approx(20)


def test_detour_costs_extra_fuel() -> None:
    off = [cand(300, 3.0, 1, detour=5.0)]
    on = [cand(300, 3.0, 1, detour=0.0)]
    plan_off = plan_stops(off, 700, FULL)
    plan_on = plan_stops(on, 700, FULL)
    replay(plan_off, off, FULL)
    assert plan_on.total_gallons == pytest.approx(20)
    assert plan_off.total_gallons == pytest.approx(21)  # 10 extra miles at 10 mpg
    assert plan_off.stops[0].fuel_on_arrival_gallons == pytest.approx(19)
    assert plan_off.stops[0].detour_miles == 5.0


def test_detour_counts_towards_reachability() -> None:
    with pytest.raises(UnreachableError) as exc:
        plan_stops([cand(495, 3.0, 1, detour=5.0)], 800, FULL)
    assert exc.value.from_miles == 0


def test_gap_raises_unreachable_with_miles() -> None:
    candidates = [cand(400, 3.0), cand(812, 3.0), cand(1340, 3.0)]
    with pytest.raises(UnreachableError) as exc:
        plan_stops(candidates, 1500, FULL)
    err = exc.value
    assert (err.from_miles, err.to_miles, err.range_miles) == (812, 1340, 500)
    assert (
        str(err) == "No fuel station within 500 miles after mile 812 (next candidate at mile 1,340)"
    )


def test_finish_beyond_last_station_raises() -> None:
    with pytest.raises(UnreachableError) as exc:
        plan_stops([cand(400, 3.0), cand(800, 3.0)], 1400, FULL)
    err = exc.value
    assert (err.from_miles, err.to_miles) == (800, 1400)
    assert err.to_finish
    assert "finish at mile 1,400" in str(err)


def test_no_station_reachable_from_start() -> None:
    with pytest.raises(UnreachableError) as exc:
        plan_stops([cand(600, 3.0)], 900, FULL)
    assert (exc.value.from_miles, exc.value.to_miles) == (0, 600)
    with pytest.raises(UnreachableError) as exc:
        plan_stops([], 900, FULL)
    assert (exc.value.from_miles, exc.value.to_miles) == (0, 900)


def test_empty_tank_start_needs_station_at_mile_zero() -> None:
    empty = PlannerConfig(initial_fuel_gallons=0)
    candidates = [cand(0.0, 3.0, 1), cand(250, 4.0, 2)]
    plan = plan_stops(candidates, 300, empty)
    replay(plan, candidates, empty)
    assert [s.station_id for s in plan.stops] == [1]
    assert plan.stops[0].gallons_bought == pytest.approx(30)
    with pytest.raises(UnreachableError) as exc:
        plan_stops([cand(5.0, 3.0)], 300, empty)
    assert (exc.value.from_miles, exc.value.to_miles) == (0, 5)


def test_same_position_duplicates_do_not_loop() -> None:
    candidates = [cand(300, 3.0, 1), cand(300, 3.0, 2), cand(300, 2.0, 3), cand(600, 4.0, 4)]
    plan = plan_stops(candidates, 1000, FULL)
    replay(plan, candidates, FULL)
    assert [s.station_id for s in plan.stops] == [3, 4]


def test_unsorted_input_is_accepted() -> None:
    candidates = [cand(700, 5.0, 3), cand(100, 3.0, 1), cand(400, 4.0, 2)]
    plan = plan_stops(candidates, 1000, PlannerConfig(initial_fuel_gallons=10))
    assert [s.station_id for s in plan.stops] == [1, 2, 3]


# --- property tests -----------------------------------------------------------------


def naive_fill_every_station(
    candidates: list[Candidate], route_miles: float, config: PlannerConfig
) -> float:
    """Visit every station in order, fill the tank at each, buy just enough at the end."""
    mpg, tank = config.mpg, config.tank_gallons
    fuel, position, cost = config.start_fuel_gallons, 0.0, 0.0
    if route_miles / mpg <= fuel + 1e-9:
        return 0.0
    for c in candidates:
        fuel -= (c.position_miles - position + 2 * c.detour_miles) / mpg
        assert fuel >= -1e-9, "naive strategy infeasible"
        position = c.position_miles
        to_finish = (route_miles - position) / mpg
        if to_finish <= tank + 1e-9:
            buy = max(0.0, to_finish - fuel)
            return cost + buy * c.price
        cost += (tank - fuel) * c.price
        fuel = tank
    raise AssertionError("naive strategy cannot reach the finish")


def random_instance(rng: random.Random, detours: bool) -> tuple[list[Candidate], float]:
    route_miles = rng.uniform(300, 3000)
    candidates: list[Candidate] = []
    position = rng.uniform(20, 200)
    while position < route_miles:
        detour = rng.uniform(0, 3) if detours else 0.0
        candidates.append(cand(position, round(rng.uniform(2.5, 6.5), 3), len(candidates), detour))
        position += rng.uniform(20, 200)
    return candidates, route_miles


@pytest.mark.parametrize("detours", [False, True])
def test_property_greedy_beats_naive_and_stays_feasible(detours: bool) -> None:
    rng = random.Random(20261025)
    for _ in range(200):
        candidates, route_miles = random_instance(rng, detours)
        first_need = (
            (candidates[0].position_miles + 2 * candidates[0].detour_miles) / 10
            if candidates
            else 0.0
        )
        initial = None if rng.random() < 0.5 else rng.uniform(min(first_need, 50), 50)
        config = PlannerConfig(initial_fuel_gallons=initial)

        plan = plan_stops(candidates, route_miles, config)
        replay(plan, candidates, config)
        positions = [s.position_miles for s in plan.stops]
        assert positions == sorted(set(positions))
        assert (
            plan.total_cost_usd <= naive_fill_every_station(candidates, route_miles, config) + 1e-6
        )
        if plan.stops:
            assert plan.fuel_at_finish_gallons == pytest.approx(0, abs=1e-6)


# --- brute-force optimality ---------------------------------------------------------

UNIT_GALLONS = 0.5  # DP fuel grid; with 10 mpg one unit is 5 miles
UNIT_MILES = 5


def dp_optimal_cost(
    candidates: list[Candidate], route_miles: float, config: PlannerConfig
) -> float:
    """Exact optimum over fuel levels on a 0.5-gallon grid (distances are grid multiples)."""
    tank_units = round(config.tank_gallons / UNIT_GALLONS)
    initial_units = round(config.start_fuel_gallons / UNIT_GALLONS)
    nodes = [(0.0, 0.0, 0.0)] + [(c.position_miles, c.detour_miles, c.price) for c in candidates]

    def leg_units(i: int, to_pos: float, to_detour: float) -> int:
        miles = to_pos - nodes[i][0] + 2 * to_detour
        return round(miles / UNIT_MILES)

    inf = math.inf
    # best[i][f]: min cost to depart node i holding f units.
    best = [[inf] * (tank_units + 1) for _ in nodes]
    best[0][initial_units] = 0.0
    for j in range(1, len(nodes)):
        pos, detour, price = nodes[j]
        arrive = [inf] * (tank_units + 1)
        for i in range(j):
            need = leg_units(i, pos, detour)
            for f in range(need, tank_units + 1):
                if best[i][f] < arrive[f - need]:
                    arrive[f - need] = best[i][f]
        unit_price = price * UNIT_GALLONS
        running = inf
        for g in range(tank_units + 1):
            running = min(running, arrive[g] - unit_price * g)
            best[j][g] = running + unit_price * g
    answer = inf
    for i in range(len(nodes)):
        need = round((route_miles - nodes[i][0]) / UNIT_MILES)
        if need <= tank_units:
            answer = min(answer, min(best[i][need:], default=inf))
    return answer


def test_greedy_matches_brute_force_on_tiny_instances() -> None:
    rng = random.Random(7)
    checked = feasible = 0
    for _ in range(400):
        route_miles = UNIT_MILES * rng.randint(60, 300)  # 300..1500 miles
        n = rng.randint(0, 6)
        positions = sorted(UNIT_MILES * rng.randint(0, route_miles // UNIT_MILES) for _ in range(n))
        candidates = [
            cand(float(p), round(rng.uniform(2.5, 6.5), 2), k) for k, p in enumerate(positions)
        ]
        initial = None if rng.random() < 0.5 else UNIT_GALLONS * rng.randint(0, 100)
        config = PlannerConfig(initial_fuel_gallons=initial)

        optimum = dp_optimal_cost(candidates, route_miles, config)
        checked += 1
        if math.isinf(optimum):
            with pytest.raises(UnreachableError):
                plan_stops(candidates, route_miles, config)
            continue
        feasible += 1
        plan = plan_stops(candidates, route_miles, config)
        replay(plan, candidates, config)
        assert plan.total_cost_usd <= optimum + 1e-6, (candidates, route_miles, initial)
    assert feasible >= 50  # the generator must exercise the feasible path too
