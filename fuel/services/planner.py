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

Initial fuel too low to move
----------------------------
Stations sit at city centroids, a mile or more off the route, so each one costs at
least its round-trip detour to reach. With ``initial_fuel_gallons`` 0 (or any amount
below that) no station is reachable even though the vehicle is beside one. If the
initial fuel reaches neither the finish nor any station, it is raised to exactly the
fuel needed to reach the nearest station, nearest by required fuel
``(position + 2 * detour) / mpg``, provided that fits in one tank; the first stop then
arrives empty and a warning says so. ``Plan.initial_fuel_gallons`` reports the raised
value and the raised fuel is not bought (it is assumed to be in the tank, like any
initial fuel). If even a full tank cannot reach a station, :class:`UnreachableError`
is raised as before.
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

Merging micro-stops
-------------------
The greedy is cost-optimal but often makes tiny top-ups (2 gallons to save cents). With
``min_stop_savings_usd > 0`` a post-pass merges such a stop B into the stop A recorded
just before it: A buys B's gallons (less the fuel B's own round-trip detour no longer
burns) and B is dropped. Fuel on arrival at every point after B is unchanged by such a
merge (A's extra purchase is exactly what B's purchase and detour used to cover), so the
only conditions are that A's departure fuel still fits the tank and A still buys > 0.
The extra cost is ``A.price * (B.gallons - 2 * B.detour / mpg) - B.price * B.gallons``;
a merge is applied when it is below ``min_stop_savings_usd`` (always when <= 0). Each
round re-simulates the plan from the purchase list, applies the cheapest admissible
merge and repeats until none is left, so the result is a fixed point in which no adjacent
pair is mergeable, and the total cost rises by less than ``min_stop_savings_usd`` per
removed stop. The first stop is never merged (there is no purchase at the start).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fuel.services.corridor import Candidate

EPSILON = 1e-9


@dataclass(frozen=True)
class PlannerConfig:
    mpg: float = 10.0
    range_miles: float = 500.0
    initial_fuel_gallons: float | None = None  # None means a full tank
    # Merge stops that save less than this (USD) into the previous stop; 0 disables.
    min_stop_savings_usd: float = 5.0

    def __post_init__(self) -> None:
        if self.mpg <= 0:
            raise ValueError("mpg must be positive")
        if self.range_miles <= 0:
            raise ValueError("range_miles must be positive")
        if self.min_stop_savings_usd < 0:
            raise ValueError("min_stop_savings_usd must not be negative")
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
    merged_stops: int = 0  # stops removed by the micro-stop merge post-pass


class UnreachableError(Exception):
    """No station (or the finish) is within range of the current position.

    When the next station lies within a mile of ``from_miles`` (so "no station within
    500 miles" would read as nonsense) but its detour still puts it out of reach,
    ``fuel_gallons`` and ``needed_gallons`` are set and the message names the fuel the
    nearest station needs instead.
    """

    def __init__(
        self,
        from_miles: float,
        to_miles: float,
        range_miles: float,
        *,
        to_finish: bool = False,
        fuel_gallons: float | None = None,
        needed_gallons: float | None = None,
    ):
        self.from_miles = from_miles
        self.to_miles = to_miles
        self.range_miles = range_miles
        self.to_finish = to_finish
        self.fuel_gallons = fuel_gallons
        self.needed_gallons = needed_gallons
        if fuel_gallons is not None and needed_gallons is not None:
            origin = "the start" if from_miles <= EPSILON else f"mile {from_miles:,.1f}"
            message = (
                f"Cannot reach any fuel station from {origin} with {fuel_gallons:,.2f} "
                f"gallons; the nearest station at mile {to_miles:,.1f} needs "
                f"{needed_gallons:,.2f} gallons"
            )
        else:
            target = "finish" if to_finish else "next candidate"
            message = (
                f"No fuel station within {range_miles:,.0f} miles after mile {from_miles:,.0f} "
                f"({target} at mile {to_miles:,.0f})"
            )
        super().__init__(message)


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

    def unreachable(from_miles: float, after_index: int, fuel_gallons: float) -> UnreachableError:
        """``fuel_gallons`` is the most fuel the vehicle can leave ``from_miles`` with."""
        # From the start every candidate is ahead (positions are >= 0); from a station,
        # only those strictly further along the route.
        ahead = [
            c
            for c in cands[after_index + 1 :]
            if after_index < 0 or c.position_miles > from_miles + EPSILON
        ]
        if not ahead:
            return UnreachableError(from_miles, route_miles, config.range_miles, to_finish=True)
        if ahead[0].position_miles <= from_miles + 1.0:
            nearest = min(ahead, key=lambda c: need_to(from_miles, c))
            return UnreachableError(
                from_miles,
                nearest.position_miles,
                config.range_miles,
                fuel_gallons=fuel_gallons,
                needed_gallons=need_to(from_miles, nearest),
            )
        return UnreachableError(from_miles, ahead[0].position_miles, config.range_miles)

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
    warnings: list[str] = []
    reachable = [i for i, c in enumerate(cands) if need_to(0.0, c) <= initial + EPSILON]
    if not reachable and cands:
        # See "Initial fuel too low to move": raise it to the minimum that reaches a station.
        nearest = min(range(len(cands)), key=lambda i: (need_to(0.0, cands[i]), i))
        needed = need_to(0.0, cands[nearest])
        if needed <= tank + EPSILON:
            warnings.append(
                f"Initial fuel raised from {initial:,.2f} to {needed:,.2f} gallons, the minimum "
                f"needed to reach the first station at mile {cands[nearest].position_miles:,.1f}; "
                "the vehicle cannot move with less."
            )
            initial = min(needed, tank)
            reachable = [i for i, c in enumerate(cands) if need_to(0.0, c) <= initial + EPSILON]
    if not reachable:
        raise unreachable(0.0, -1, initial)
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
            raise unreachable(pos, current, tank)
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

    merged = 0
    if config.min_stop_savings_usd > 0 and len(stops) >= 2:
        stops, merged, merged_finish = _merge_micro_stops(
            stops, config, initial, need_to, need_to_finish
        )
        if merged:
            fuel_at_finish = merged_finish

    if merged:
        threshold = f"${config.min_stop_savings_usd:,.2f}"
        what = (
            f"1 stop that saved less than {threshold} was"
            if merged == 1
            else f"{merged} stops that each saved less than {threshold} were"
        )
        warnings.append(
            f"{what} merged into the previous stop "
            "(set min_stop_savings_usd=0 for the pure cost optimum)"
        )

    return Plan(
        stops=tuple(stops),
        total_gallons=sum(s.gallons_bought for s in stops),
        total_cost_usd=sum(s.cost_usd for s in stops),
        route_miles=route_miles,
        initial_fuel_gallons=initial,
        fuel_at_finish_gallons=fuel_at_finish,
        warnings=tuple(warnings),
        merged_stops=merged,
    )


def _simulate(
    purchases: Sequence[tuple[Candidate, float]],
    config: PlannerConfig,
    initial_fuel_gallons: float,
    need_to: Callable[[float, Candidate], float],
    need_to_finish: Callable[[float], float],
) -> tuple[list[Stop], float]:
    """Drive from the start buying ``purchases`` in order; return the stops and the fuel
    left at the finish. Fuel needs are computed exactly as in the greedy."""
    tank = config.tank_gallons
    fuel = initial_fuel_gallons
    previous = 0.0
    stops: list[Stop] = []
    for order, (c, gallons) in enumerate(purchases, start=1):
        arrival = max(0.0, fuel - need_to(previous, c))
        departure = min(tank, arrival + gallons)
        stops.append(
            Stop(
                station_id=c.station_id,
                position_miles=c.position_miles,
                detour_miles=c.detour_miles,
                price_per_gallon=c.price,
                gallons_bought=gallons,
                cost_usd=gallons * c.price,
                fuel_on_arrival_gallons=arrival,
                fuel_on_departure_gallons=departure,
                order=order,
            )
        )
        fuel, previous = departure, c.position_miles
    return stops, max(0.0, fuel - need_to_finish(previous))


def _merge_micro_stops(
    stops: list[Stop],
    config: PlannerConfig,
    initial_fuel_gallons: float,
    need_to: Callable[[float, Candidate], float],
    need_to_finish: Callable[[float], float],
) -> tuple[list[Stop], int, float]:
    """Merge stops saving less than ``config.min_stop_savings_usd`` into the previous stop.

    Returns the new stops, the number of stops removed and the fuel left at the finish.
    See "Merging micro-stops" in the module docstring for why each merge is feasible.
    """
    threshold = config.min_stop_savings_usd
    tank = config.tank_gallons
    purchases = [
        (
            Candidate(
                position_miles=s.position_miles,
                price=s.price_per_gallon,
                station_id=s.station_id,
                detour_miles=s.detour_miles,
            ),
            s.gallons_bought,
        )
        for s in stops
    ]
    current, fuel_at_finish = stops, 0.0
    merged = 0
    while True:
        best: tuple[float, int, float] | None = None
        for k in range(1, len(current)):  # k >= 1: the first stop is never merged
            a, b = current[k - 1], current[k]
            # B's round-trip detour is no longer driven, so A needs that much less.
            transfer = b.gallons_bought - 2.0 * b.detour_miles / config.mpg
            if a.fuel_on_departure_gallons + transfer > tank + EPSILON:
                continue  # A's tank cannot hold B's purchase
            if a.gallons_bought + transfer <= EPSILON:
                continue  # A would no longer buy anything
            extra = a.price_per_gallon * transfer - b.price_per_gallon * b.gallons_bought
            if extra < threshold and (best is None or extra < best[0]):
                best = (extra, k, transfer)
        if best is None:
            break
        _, k, transfer = best
        a_candidate, a_gallons = purchases[k - 1]
        purchases[k - 1] = (a_candidate, a_gallons + transfer)
        del purchases[k]
        merged += 1
        current, fuel_at_finish = _simulate(
            purchases, config, initial_fuel_gallons, need_to, need_to_finish
        )
    return current, merged, fuel_at_finish
