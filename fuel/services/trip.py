"""Trip orchestration: resolve locations -> route -> corridor -> planner -> stations.

This is the single entry point the API and the map page call. It owns timing and the
external-call counter for one request; it raises the services' typed errors
(``GeocodingError``, ``RoutingError``, ``UnreachableError``) unchanged so the API layer
can map them to HTTP responses.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from django.conf import settings

from fuel.models import FuelStation
from fuel.services.corridor import Candidate, find_candidates, route_length_miles
from fuel.services.gazetteer import get_station_index
from fuel.services.geocoding import Location, resolve_location
from fuel.services.http import get_external_calls, reset_external_calls
from fuel.services.planner import Plan, PlannerConfig, plan_stops
from fuel.services.routing import Route, get_route


@dataclass(frozen=True)
class TripResult:
    start_query: str
    finish_query: str
    start: Location
    finish: Location
    route: Route
    plan: Plan
    config: PlannerConfig
    corridor_miles: float
    stations: dict[int, FuelStation]  # FuelStation pk -> station (with its place loaded)
    candidate_count: int
    external_calls: int
    routing_ms: float
    compute_ms: float  # wall time of the whole call minus routing_ms


def _scale_candidates(candidates: list[Candidate], scale: float) -> list[Candidate]:
    if abs(scale - 1.0) < 1e-12:
        return candidates
    return [dataclasses.replace(c, position_miles=c.position_miles * scale) for c in candidates]


def plan_trip(
    start_text: str,
    finish_text: str,
    *,
    corridor_miles: float | None = None,
    initial_fuel_gallons: float | None = None,
    mpg: float | None = None,
    range_miles: float | None = None,
    min_stop_savings_usd: float | None = None,
) -> TripResult:
    """Plan the cheapest fuel stops between two free-text (or "lat,lon") locations."""
    reset_external_calls()
    started = time.perf_counter()

    corridor = float(settings.FUEL_CORRIDOR_MILES if corridor_miles is None else corridor_miles)
    config = PlannerConfig(
        mpg=float(settings.FUEL_MPG if mpg is None else mpg),
        range_miles=float(settings.FUEL_RANGE_MILES if range_miles is None else range_miles),
        initial_fuel_gallons=initial_fuel_gallons,
        min_stop_savings_usd=float(
            PlannerConfig.min_stop_savings_usd
            if min_stop_savings_usd is None
            else min_stop_savings_usd
        ),
    )

    start = resolve_location(start_text)
    finish = resolve_location(finish_text)

    routing_started = time.perf_counter()
    route = get_route((start.lat, start.lon), (finish.lat, finish.lon))
    routing_ms = (time.perf_counter() - routing_started) * 1000.0

    candidates = find_candidates(route.coordinates, get_station_index(), corridor)

    # Candidate positions are mile markers along the polyline (haversine between vertices),
    # while the planner's route length is OSRM's road distance. The two differ slightly
    # (OSRM measures along the road network; the polyline is a simplification), so scale
    # the positions onto OSRM's mile scale: the last station must not appear to lie
    # beyond the finish, and fuel needed to finish must match the reported distance.
    # The factor is ~1.0 for real routes (`overview=full` geometry).
    polyline_miles = route_length_miles(route.coordinates)
    scale = route.distance_miles / polyline_miles if polyline_miles > 0 else 1.0
    candidates = _scale_candidates(candidates, scale)

    plan = plan_stops(candidates, route.distance_miles, config)

    stations = FuelStation.objects.select_related("place").in_bulk(
        [stop.station_id for stop in plan.stops]
    )

    total_ms = (time.perf_counter() - started) * 1000.0
    return TripResult(
        start_query=start_text,
        finish_query=finish_text,
        start=start,
        finish=finish,
        route=route,
        plan=plan,
        config=config,
        corridor_miles=corridor,
        stations=stations,
        candidate_count=len(candidates),
        external_calls=get_external_calls(),
        routing_ms=routing_ms,
        compute_ms=max(0.0, total_ms - routing_ms),
    )
