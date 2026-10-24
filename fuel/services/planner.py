"""Cost-minimising fuel-stop planner (the single-vehicle gas-station problem).

Pure Python, no Django and no I/O: constants arrive in :class:`PlannerConfig`.

Fuel model
----------
Candidates are stations projected onto the route (``position_miles``) with a
straight-line ``detour_miles``. Driving from a point at mile ``a`` (the start, or a
station already visited) to a station at mile ``b`` with detour ``d`` consumes
``(b - a + 2 * d) / mpg`` gallons: the round-trip detour is charged on the way to that
station. Driving from a station at mile ``b`` to the finish consumes
``(route_miles - b) / mpg`` gallons.

Greedy
------
The start behaves like a free station holding ``initial_fuel_gallons``, so the first stop
is the cheapest station reachable on the initial fuel (zero stops if the finish is).
At each stop with price ``p``, among the stations ahead that are reachable on a full tank:

1. if one is cheaper than ``p``, buy just enough to reach the first such station;
2. otherwise, if the finish is reachable on a full tank, buy just enough to finish empty;
3. otherwise, if none is reachable, raise :class:`UnreachableError`;
4. otherwise fill the tank and go to the cheapest reachable station (tie: the nearest).

Step 1 is checked before step 2 (SPEC lists the finish check first). Treating the finish
as a station with price 0 is what makes the greedy optimal: with the finish check first,
a stop that can reach the finish would buy all remaining fuel at its own price even when
a cheaper station lies between it and the finish.

The greedy is provably optimal when every ``detour_miles`` is 0. With detours it remains
feasible and consumption is exact, but choosing stations does not weigh their detour
cost, so it is a heuristic: on random tiny instances with detours of 0-10 miles it was
on average $0.65 above the exact optimum (worst case seen: $18).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fuel.services.corridor import Candidate

EPSILON = 1e-9


@dataclass(frozen=True)
class PlannerConfig:
    mpg: float = 10.0
    range_miles: float = 500.0
    initial_fuel_gallons: float | None = None  # None means a full tank

    def __post_init__(self) -> None:
        if self.mpg <= 0:
            raise ValueError("mpg must be positive")
        if self.range_miles <= 0:
            raise ValueError("range_miles must be positive")
        initial = self.initial_fuel_gallons
        if initial is not None and not (0 <= initial <= self.tank_gallons + EPSILON):
            raise ValueError(f"initial_fuel_gallons must be between 0 and {self.tank_gallons:g}")

    @property
    def tank_gallons(self) -> float:
        return self.range_miles / self.mpg

    @property
    def start_fuel_gallons(self) -> float:
        if self.initial_fuel_gallons is None:
            return self.tank_gallons
        return min(float(self.initial_fuel_gallons), self.tank_gallons)


@dataclass(frozen=True)
class Stop:
    station_id: int
    position_miles: float
    detour_miles: float
    price_per_gallon: float
    gallons_bought: float
    cost_usd: float
    fuel_on_arrival_gallons: float
    fuel_on_departure_gallons: float
    order: int


@dataclass(frozen=True)
class Plan:
    stops: tuple[Stop, ...]
    total_gallons: float
    total_cost_usd: float
    route_miles: float
    initial_fuel_gallons: float
    fuel_at_finish_gallons: float
    warnings: tuple[str, ...]


class UnreachableError(Exception):
    """No station (or the finish) is within range of the current position."""

    def __init__(
        self, from_miles: float, to_miles: float, range_miles: float, *, to_finish: bool = False
    ):
        self.from_miles = from_miles
        self.to_miles = to_miles
        self.range_miles = range_miles
        self.to_finish = to_finish
        target = "finish" if to_finish else "next candidate"
        super().__init__(
            f"No fuel station within {range_miles:,.0f} miles after mile {from_miles:,.0f} "
            f"({target} at mile {to_miles:,.0f})"
        )


def _gallons(miles: float, mpg: float) -> float:
    return max(0.0, miles) / mpg


def plan_stops(candidates: Sequence[Candidate], route_miles: float, config: PlannerConfig) -> Plan:
    """Choose fuel stops minimising total fuel cost for a route of ``route_miles``."""
    mpg = config.mpg
    tank = config.tank_gallons
    initial = config.start_fuel_gallons
    route_miles = max(0.0, float(route_miles))
    cands = sorted(candidates)

    def need_to(from_miles: float, c: Candidate) -> float:
        return _gallons(c.position_miles - from_miles + 2.0 * c.detour_miles, mpg)

    def need_to_finish(from_miles: float) -> float:
        return _gallons(route_miles - from_miles, mpg)

    def unreachable(from_miles: float, after_index: int) -> UnreachableError:
        for c in cands[after_index + 1 :]:
            if c.position_miles > from_miles + EPSILON:
                return UnreachableError(
                    from_miles, c.position_miles, config.range_miles, to_finish=False
                )
        return UnreachableError(from_miles, route_miles, config.range_miles, to_finish=True)

    # 1. Coast on the initial fuel.
    if need_to_finish(0.0) <= initial + EPSILON:
        return Plan(
            stops=(),
            total_gallons=0.0,
            total_cost_usd=0.0,
            route_miles=route_miles,
            initial_fuel_gallons=initial,
            fuel_at_finish_gallons=max(0.0, initial - need_to_finish(0.0)),
            warnings=("The finish is reachable on the initial fuel; no fuel stops are needed.",),
        )
    reachable = [i for i, c in enumerate(cands) if need_to(0.0, c) <= initial + EPSILON]
    if not reachable:
        raise unreachable(0.0, -1)
    # Cheapest; ties go to the nearest (candidates are sorted by position).
    current = min(reachable, key=lambda i: (cands[i].price, i))
    fuel = max(0.0, initial - need_to(0.0, cands[current]))

    stops: list[Stop] = []
    while True:
        here = cands[current]
        pos = here.position_miles
        # Ahead: later in the sorted list and strictly further along the route. Stations
        # at the same mile are skipped; any of them later in the list is no cheaper.
        ahead: list[int] = []
        for j in range(current + 1, len(cands)):
            c = cands[j]
            if c.position_miles - pos > config.range_miles + EPSILON:
                break  # sorted by position: nothing further can be in range
            if c.position_miles > pos + EPSILON and need_to(pos, c) <= tank + EPSILON:
                ahead.append(j)
        cheaper = next((j for j in ahead if cands[j].price < here.price), None)

        if cheaper is not None:
            target: int | None = cheaper
            buy = max(0.0, need_to(pos, cands[cheaper]) - fuel)
        elif need_to_finish(pos) <= tank + EPSILON:
            target = None
            buy = max(0.0, need_to_finish(pos) - fuel)
        elif not ahead:
            raise unreachable(pos, current)
        else:
            target = min(ahead, key=lambda j: (cands[j].price, j))
            buy = max(0.0, tank - fuel)

        departure = min(tank, fuel + buy)
        # Every visited station gets a purchase > 0 (up to float noise). The start and each
        # fill-up stop pick the cheapest station in their reach, so a station that is
        # cheaper than a later stop, or the finish, cannot already be reachable on the fuel
        # left on arrival: it would have been in the earlier reach (detours only add
        # distance) and chosen instead. After a "just enough" purchase the tank arrives
        # empty, so the next leg always needs fuel. The only exceptions are float noise
        # and a zero-detour station at mile 0 reached on a full tank; in those cases the
        # vehicle simply drives past and no Stop is recorded.
        if buy > EPSILON:
            stops.append(
                Stop(
                    station_id=here.station_id,
                    position_miles=pos,
                    detour_miles=here.detour_miles,
                    price_per_gallon=here.price,
                    gallons_bought=buy,
                    cost_usd=buy * here.price,
                    fuel_on_arrival_gallons=fuel,
                    fuel_on_departure_gallons=departure,
                    order=len(stops) + 1,
                )
            )

        if target is None:
            fuel_at_finish = max(0.0, departure - need_to_finish(pos))
            break
        fuel = max(0.0, departure - need_to(pos, cands[target]))
        current = target

    return Plan(
        stops=tuple(stops),
        total_gallons=sum(s.gallons_bought for s in stops),
        total_cost_usd=sum(s.cost_usd for s in stops),
        route_miles=route_miles,
        initial_fuel_gallons=initial,
        fuel_at_finish_gallons=fuel_at_finish,
        warnings=(),
    )
