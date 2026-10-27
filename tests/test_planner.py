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


def test_empty_tank_start_uses_station_at_mile_zero_without_adjustment() -> None:
    empty = PlannerConfig(initial_fuel_gallons=0)
    candidates = [cand(0.0, 3.0, 1), cand(250, 4.0, 2)]
    plan = plan_stops(candidates, 300, empty)
    replay(plan, candidates, empty)
    assert plan.initial_fuel_gallons == 0
    assert [s.station_id for s in plan.stops] == [1]
    assert plan.stops[0].gallons_bought == pytest.approx(30)
    assert not any("raised" in w for w in plan.warnings)


def test_empty_tank_start_raises_initial_fuel_to_reach_nearest_station() -> None:
    # Stations sit off the route: with 0 gallons none is reachable, so the initial fuel
    # is raised to exactly what the nearest one (by required fuel) needs.
    empty = PlannerConfig(initial_fuel_gallons=0)
    candidates = [cand(1.2, 3.0, 1, detour=1.5), cand(3.0, 2.9, 2, detour=0.4), cand(250, 4.0, 3)]
    plan = plan_stops(candidates, 300, empty)
    replay(plan, candidates, empty)
    # Needs: station 1 (1.2 + 3.0) / 10 = 0.42, station 2 (3.0 + 0.8) / 10 = 0.38.
    assert plan.initial_fuel_gallons == pytest.approx(0.38)
    assert plan.warnings[0] == (
        "Initial fuel raised from 0.00 to 0.38 gallons, the minimum needed to reach the "
        "first station at mile 3.0; the vehicle cannot move with less."
    )
    first = plan.stops[0]
    assert first.station_id == 2
    assert first.fuel_on_arrival_gallons == pytest.approx(0)
    # Every mile driven, detours included, is paid for except the raised initial fuel.
    trip_gallons = (300 + sum(2 * s.detour_miles for s in plan.stops)) / 10
    assert plan.total_gallons == pytest.approx(trip_gallons - plan.initial_fuel_gallons)
    assert plan.total_cost_usd == pytest.approx(
        sum(s.gallons_bought * s.price_per_gallon for s in plan.stops)
    )
    assert plan.fuel_at_finish_gallons == pytest.approx(0)


def test_low_initial_fuel_is_raised_with_merging_enabled() -> None:
    config = PlannerConfig(initial_fuel_gallons=0.1)  # default $5 merge threshold
    candidates = [cand(1.2, 3.0, 1, detour=1.5), cand(450, 3.5, 2), cand(480, 3.4, 3)]
    plan = plan_stops(candidates, 900, config)
    replay(plan, candidates, config)
    assert plan.initial_fuel_gallons == pytest.approx(0.42)
    assert plan.warnings[0].startswith("Initial fuel raised from 0.10 to 0.42 gallons")
    assert plan.stops[0].fuel_on_arrival_gallons == pytest.approx(0)


def test_empty_tank_start_without_station_in_one_tank_raises() -> None:
    empty = PlannerConfig(initial_fuel_gallons=0)
    with pytest.raises(UnreachableError) as exc:
        plan_stops([cand(600, 3.0)], 900, empty)
    assert (exc.value.from_miles, exc.value.to_miles) == (0, 600)
    assert str(exc.value) == (
        "No fuel station within 500 miles after mile 0 (next candidate at mile 600)"
    )
    # A station next to the start but with a detour longer than half a tank.
    with pytest.raises(UnreachableError) as exc:
        plan_stops([cand(0.5, 3.0, 1, detour=260.0)], 900, empty)
    err = exc.value
    assert (err.from_miles, err.to_miles) == (0, 0.5)
    assert err.fuel_gallons == 0
    assert err.needed_gallons == pytest.approx(52.05)
    assert str(err) == (
        "Cannot reach any fuel station from the start with 0.00 gallons; "
        "the nearest station at mile 0.5 needs 52.05 gallons"
    )
    assert "next candidate at mile 0" not in str(err)


def test_same_position_duplicates_do_not_loop() -> None:
    candidates = [cand(300, 3.0, 1), cand(300, 3.0, 2), cand(300, 2.0, 3), cand(600, 4.0, 4)]
    plan = plan_stops(candidates, 1000, FULL)
    replay(plan, candidates, FULL)
    assert [s.station_id for s in plan.stops] == [3, 4]


# --- merging micro-stops ------------------------------------------------------------

MICRO = [cand(100, 3.50, 1), cand(400, 3.40, 2), cand(420, 3.20, 3)]
MICRO_START = 10.0  # only station 1 is reachable at the start


def test_micro_stop_is_merged_into_previous_stop() -> None:
    pure_config = PlannerConfig(initial_fuel_gallons=MICRO_START, min_stop_savings_usd=0)
    pure = plan_stops(MICRO, 900, pure_config)
    replay(pure, MICRO, pure_config)
    # Greedy: 30 gal at 1 to reach 2, 2 gal at 2 to reach 3 (saves $0.20), 48 gal at 3.
    assert [s.station_id for s in pure.stops] == [1, 2, 3]
    assert [s.gallons_bought for s in pure.stops] == pytest.approx([30, 2, 48])
    assert pure.merged_stops == 0
    assert pure.warnings == ()

    config = PlannerConfig(initial_fuel_gallons=MICRO_START)  # default: $5
    plan = plan_stops(MICRO, 900, config)
    replay(plan, MICRO, config)
    assert [s.station_id for s in plan.stops] == [1, 3]
    assert [s.order for s in plan.stops] == [1, 2]
    a, c = plan.stops
    assert a.gallons_bought == pytest.approx(32)
    assert a.fuel_on_departure_gallons == pytest.approx(32)
    assert c.fuel_on_arrival_gallons == pytest.approx(0)
    assert c.gallons_bought == pytest.approx(48)
    assert plan.merged_stops == 1
    assert plan.total_gallons == pytest.approx(pure.total_gallons)
    assert plan.total_cost_usd - pure.total_cost_usd == pytest.approx(0.20)
    assert plan.total_cost_usd - pure.total_cost_usd <= 5.0 * plan.merged_stops
    assert plan.fuel_at_finish_gallons == pytest.approx(0)
    assert plan.warnings == (
        "1 stop that saved less than $5.00 was merged into a neighbouring stop "
        "(set min_stop_savings_usd=0 to disable merging)",
    )


def test_merge_warning_counts_several_stops() -> None:
    # Two micro-stops after station 1, both cheap to absorb, all within one tank.
    candidates = [cand(100, 3.50, 1), cand(300, 3.45, 2), cand(320, 3.40, 3), cand(340, 3.0, 4)]
    config = PlannerConfig(initial_fuel_gallons=MICRO_START)
    pure = plan_stops(
        candidates, 800, PlannerConfig(initial_fuel_gallons=MICRO_START, min_stop_savings_usd=0)
    )
    plan = plan_stops(candidates, 800, config)
    replay(plan, candidates, config)
    assert [s.station_id for s in pure.stops] == [1, 2, 3, 4]
    assert [s.station_id for s in plan.stops] == [1, 4]
    assert plan.merged_stops == 2
    assert plan.total_cost_usd - pure.total_cost_usd <= 5.0 * 2
    assert plan.warnings[0].startswith("2 stops that each saved less than $5.00 were merged")


def test_merge_respects_tank_capacity() -> None:
    # Station 1 buys 49 gallons to reach 2; station 2's 2-gallon top-up saves only $0.02
    # but would overflow station 1's tank (49 + 2 > 50), so it stays. Station 3 saves
    # $19 over buying at 2, so it stays too.
    candidates = [cand(100, 3.50, 1), cand(590, 3.49, 2), cand(610, 3.00, 3)]
    config = PlannerConfig(initial_fuel_gallons=MICRO_START)
    plan = plan_stops(candidates, 1000, config)
    replay(plan, candidates, config)
    assert [s.station_id for s in plan.stops] == [1, 2, 3]
    assert [s.gallons_bought for s in plan.stops] == pytest.approx([49, 2, 39])
    assert plan.merged_stops == 0
    assert plan.warnings == ()


def test_merge_with_large_threshold_never_overfills() -> None:
    candidates = [cand(100, 3.50, 1), cand(590, 3.49, 2), cand(610, 3.00, 3)]
    config = PlannerConfig(initial_fuel_gallons=MICRO_START, min_stop_savings_usd=50)
    plan = plan_stops(candidates, 1000, config)
    replay(plan, candidates, config)
    # 3 merges into 2 (2 + 39 fits); 2 can never merge into 1 (49 + 41 > 50).
    assert [s.station_id for s in plan.stops] == [1, 2]
    assert [s.gallons_bought for s in plan.stops] == pytest.approx([49, 41])
    assert plan.merged_stops == 1


def test_merge_credits_the_detour_no_longer_driven() -> None:
    candidates = [cand(100, 3.50, 1), cand(400, 3.40, 2, detour=0.5), cand(420, 3.20, 3)]
    pure_config = PlannerConfig(initial_fuel_gallons=MICRO_START, min_stop_savings_usd=0)
    pure = plan_stops(candidates, 900, pure_config)
    assert [s.gallons_bought for s in pure.stops] == pytest.approx([30.1, 2, 48])
    config = PlannerConfig(initial_fuel_gallons=MICRO_START)
    plan = plan_stops(candidates, 900, config)
    replay(plan, candidates, config)
    assert [s.station_id for s in plan.stops] == [1, 3]
    # Station 1 needs 1.9 more gallons: station 2's 2 gallons less its 0.1-gallon detour.
    assert plan.stops[0].gallons_bought == pytest.approx(32.0)
    assert plan.total_gallons == pytest.approx(pure.total_gallons - 0.1)
    assert plan.fuel_at_finish_gallons == pytest.approx(0)


def test_first_stop_is_never_merged_into_the_start() -> None:
    plan = plan_stops([cand(450, 3.0, 1)], 520, FULL)  # buys 2 gallons
    assert [s.gallons_bought for s in plan.stops] == pytest.approx([2])
    assert plan.merged_stops == 0


# Start full. Station 1 (2.80) fills 40 gallons and heads for the nearer of the two 3.10
# stations; station 2 is then 2 gallons short of full and fills up to reach station 3.
FORWARD = [cand(400, 2.80, 1), cand(420, 3.10, 2), cand(500, 3.10, 3), cand(900, 4.00, 4)]


def test_micro_stop_after_full_tank_is_merged_forward_at_no_cost() -> None:
    pure_config = PlannerConfig(min_stop_savings_usd=0)
    pure = plan_stops(FORWARD, 1300, pure_config)
    replay(pure, FORWARD, pure_config)
    assert [s.station_id for s in pure.stops] == [1, 2, 3, 4]
    assert [s.gallons_bought for s in pure.stops] == pytest.approx([40, 2, 8, 30])
    assert [s.fuel_on_arrival_gallons for s in pure.stops] == pytest.approx([10, 48, 42, 10])
    # 40 * 2.80 + 2 * 3.10 + 8 * 3.10 + 30 * 4.00
    assert pure.total_cost_usd == pytest.approx(263.0)
    assert pure.merged_stops == 0
    assert pure.warnings == ()

    # Station 2 cannot go backward (station 1 leaves full: 50 + 2 > 50), but station 3
    # arrives with 42 gallons, so it can buy station 2's 2 gallons at the same price.
    # Station 3 into 4 would cost 0.90 * 8 = $7.20, station 1 into 2 0.30 * 40 = $12.
    config = PlannerConfig()
    plan = plan_stops(FORWARD, 1300, config)
    replay(plan, FORWARD, config)
    assert [s.station_id for s in plan.stops] == [1, 3, 4]
    assert [s.order for s in plan.stops] == [1, 2, 3]
    assert [s.gallons_bought for s in plan.stops] == pytest.approx([40, 10, 30])
    assert [s.fuel_on_arrival_gallons for s in plan.stops] == pytest.approx([10, 40, 10])
    assert [s.fuel_on_departure_gallons for s in plan.stops] == pytest.approx([50, 50, 40])
    assert plan.merged_stops == 1
    assert plan.total_cost_usd == pytest.approx(pure.total_cost_usd)
    assert plan.total_gallons == pytest.approx(pure.total_gallons)
    assert plan.fuel_at_finish_gallons == pytest.approx(0)
    assert plan.warnings == (
        "1 stop that saved less than $5.00 was merged into a neighbouring stop "
        "(set min_stop_savings_usd=0 to disable merging)",
    )


def test_forward_merge_needs_enough_fuel_on_arrival_at_the_next_stop() -> None:
    # As FORWARD, but the next 3.10 station is so far that station 2 fills up to reach it.
    def stations(third_miles: float) -> list[Candidate]:
        return [cand(400, 2.80, 1), cand(420, 3.10, 2), cand(third_miles, 3.10, 3)]

    config = PlannerConfig()
    # Mile 905: station 3 arrives with 50 - 48.5 = 1.5 gallons, less than station 2's
    # 2 gallons, so dropping station 2 would run dry before 3 even though it costs $0.
    candidates = stations(905)
    plan = plan_stops(candidates, 1300, config)
    replay(plan, candidates, config)
    assert [s.station_id for s in plan.stops] == [1, 2, 3]
    assert [s.gallons_bought for s in plan.stops] == pytest.approx([40, 2, 38])
    assert plan.stops[2].fuel_on_arrival_gallons == pytest.approx(1.5)
    assert plan.merged_stops == 0
    assert plan.warnings == ()

    # Mile 900: station 3 arrives with exactly 2 gallons, so the merge fits and station 3
    # arrives empty.
    candidates = stations(900)
    plan = plan_stops(candidates, 1300, config)
    replay(plan, candidates, config)
    assert [s.station_id for s in plan.stops] == [1, 3]
    assert [s.gallons_bought for s in plan.stops] == pytest.approx([40, 40])
    assert plan.stops[1].fuel_on_arrival_gallons == pytest.approx(0)
    assert plan.merged_stops == 1


def test_first_stop_can_be_merged_forward() -> None:
    # Start full. Station 1 (mile 20, 1-mile detour) is cheapest: it arrives with
    # 50 - 2.2 = 47.8 gallons and buys 2.2 to fill up for station 2 (46 gallons away),
    # which buys 38 to finish 420 miles later.
    candidates = [cand(20, 3.00, 1, detour=1.0), cand(480, 3.10, 2)]
    pure_config = PlannerConfig(min_stop_savings_usd=0)
    pure = plan_stops(candidates, 900, pure_config)
    replay(pure, candidates, pure_config)
    assert [s.gallons_bought for s in pure.stops] == pytest.approx([2.2, 38])
    assert pure.stops[1].fuel_on_arrival_gallons == pytest.approx(4)
    assert pure.total_cost_usd == pytest.approx(2.2 * 3.00 + 38 * 3.10)  # 124.40

    # Skipping station 1 also skips its 0.2-gallon detour, so station 2 arrives with
    # 50 - 48 = 2 gallons and buys 40: 40 * 3.10 = 124.00, $0.40 cheaper.
    config = PlannerConfig()
    plan = plan_stops(candidates, 900, config)
    replay(plan, candidates, config)
    assert [s.station_id for s in plan.stops] == [2]
    assert plan.stops[0].order == 1
    assert plan.stops[0].fuel_on_arrival_gallons == pytest.approx(2)
    assert plan.stops[0].gallons_bought == pytest.approx(40)
    assert plan.total_cost_usd == pytest.approx(124.0)
    assert plan.total_gallons == pytest.approx(pure.total_gallons - 0.2)
    assert plan.fuel_at_finish_gallons == pytest.approx(0)
    assert plan.merged_stops == 1


def test_zero_threshold_keeps_the_pure_greedy() -> None:
    config = PlannerConfig(min_stop_savings_usd=0)
    plan = plan_stops(FORWARD, 1300, config)
    assert [s.station_id for s in plan.stops] == [1, 2, 3, 4]
    assert plan.merged_stops == 0
    assert plan.warnings == ()
    candidates = [cand(20, 3.00, 1, detour=1.0), cand(480, 3.10, 2)]
    plan = plan_stops(candidates, 900, config)
    assert [s.station_id for s in plan.stops] == [1, 2]
    assert plan.merged_stops == 0


def test_min_stop_savings_must_not_be_negative() -> None:
    with pytest.raises(ValueError):
        PlannerConfig(min_stop_savings_usd=-1)


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


def assert_threshold_fixed_point(plan: Plan, config: PlannerConfig) -> None:
    """The merge post-pass stops at a fixed point: no stop is still mergeable into either
    neighbour."""
    tank = config.tank_gallons
    for a, b in zip(plan.stops, plan.stops[1:], strict=False):
        # Backward: B into A.
        transfer = b.gallons_bought - 2 * b.detour_miles / config.mpg
        fits = a.fuel_on_departure_gallons + transfer <= tank + 1e-9
        buys = a.gallons_bought + transfer > 1e-9
        extra = a.price_per_gallon * transfer - b.price_per_gallon * b.gallons_bought
        assert not (fits and buys and extra < config.min_stop_savings_usd), (a, b)
        # Forward: A into B.
        transfer = a.gallons_bought - 2 * a.detour_miles / config.mpg
        fits = b.fuel_on_arrival_gallons - transfer >= -1e-9
        buys = b.gallons_bought + transfer > 1e-9
        extra = b.price_per_gallon * transfer - a.price_per_gallon * a.gallons_bought
        assert not (fits and buys and extra < config.min_stop_savings_usd), (a, b)


@pytest.mark.parametrize("min_savings", [0.0, 5.0, 50.0])
@pytest.mark.parametrize("detours", [False, True])
def test_property_greedy_beats_naive_and_stays_feasible(detours: bool, min_savings: float) -> None:
    rng = random.Random(20261025)
    merged_total = 0
    for _ in range(200):
        candidates, route_miles = random_instance(rng, detours)
        first_need = (
            (candidates[0].position_miles + 2 * candidates[0].detour_miles) / 10
            if candidates
            else 0.0
        )
        initial = None if rng.random() < 0.5 else rng.uniform(min(first_need, 50), 50)
        config = PlannerConfig(initial_fuel_gallons=initial, min_stop_savings_usd=min_savings)
        pure = plan_stops(
            candidates,
            route_miles,
            PlannerConfig(initial_fuel_gallons=initial, min_stop_savings_usd=0),
        )

        plan = plan_stops(candidates, route_miles, config)
        replay(plan, candidates, config)
        positions = [s.position_miles for s in plan.stops]
        assert positions == sorted(set(positions))
        if plan.stops:
            assert plan.fuel_at_finish_gallons == pytest.approx(0, abs=1e-6)

        # Merging only removes stops (the first one included, when merged forward).
        kept = {s.station_id for s in plan.stops}
        removed = [s for s in pure.stops if s.station_id not in kept]
        assert kept <= {s.station_id for s in pure.stops}
        assert plan.merged_stops == len(removed) == len(pure.stops) - len(plan.stops)
        if pure.stops:
            assert plan.stops  # the last remaining stop can never be merged forward
        # Gallons are only redistributed, less the round-trip detours no longer driven.
        saved = sum(2 * s.detour_miles / config.mpg for s in removed)
        assert plan.total_gallons == pytest.approx(pure.total_gallons - saved, abs=1e-6)
        assert plan.total_cost_usd <= pure.total_cost_usd + min_savings * len(removed) + 1e-6
        if min_savings == 0:
            assert plan == pure
            assert (
                plan.total_cost_usd
                <= naive_fill_every_station(candidates, route_miles, config) + 1e-6
            )
        else:
            assert_threshold_fixed_point(plan, config)
        assert bool(plan.merged_stops) == any("merged" in w for w in plan.warnings)
        merged_total += plan.merged_stops
    if min_savings > 0:
        assert merged_total > 0  # the generator must exercise merging


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


def effective_initial_gallons(
    candidates: list[Candidate], route_miles: float, config: PlannerConfig
) -> float:
    """The initial fuel after the planner's "too low to move" rule (see planner docs)."""
    start = config.start_fuel_gallons
    needs = [(c.position_miles + 2 * c.detour_miles) / config.mpg for c in candidates]
    if route_miles / config.mpg <= start or not needs or min(needs) <= start:
        return start
    return min(needs) if min(needs) <= config.tank_gallons else start


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
        config = PlannerConfig(initial_fuel_gallons=initial, min_stop_savings_usd=0)
        effective = effective_initial_gallons(candidates, route_miles, config)
        if effective != config.start_fuel_gallons:
            initial = effective  # the planner raises it; compare against that optimum
        optimum = dp_optimal_cost(
            candidates,
            route_miles,
            PlannerConfig(initial_fuel_gallons=initial, min_stop_savings_usd=0),
        )
        checked += 1
        if math.isinf(optimum):
            with pytest.raises(UnreachableError):
                plan_stops(candidates, route_miles, config)
            continue
        feasible += 1
        plan = plan_stops(candidates, route_miles, config)
        replay(plan, candidates, config)
        assert plan.initial_fuel_gallons == pytest.approx(effective)
        assert plan.total_cost_usd <= optimum + 1e-6, (candidates, route_miles, initial)
    assert feasible >= 50  # the generator must exercise the feasible path too
