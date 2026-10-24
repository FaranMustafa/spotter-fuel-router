"""Request validation (DRF serializer) and response building for the route endpoints."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.conf import settings
from rest_framework import serializers

from fuel.services.geometry import DEFAULT_TOLERANCE_DEG, simplify
from fuel.services.planner import Stop
from fuel.services.trip import TripResult

# "simplified": Douglas-Peucker at DEFAULT_TOLERANCE_DEG (~50 m); "full": every OSRM
# vertex; "none": ``route.geometry`` is null. The planner always uses the full geometry.
GEOMETRY_CHOICES = ("simplified", "full", "none")


class RouteQuerySerializer(serializers.Serializer):
    """Validates the ``/api/route/`` inputs from a query string or a JSON body."""

    start = serializers.CharField(max_length=200)
    finish = serializers.CharField(max_length=200)
    corridor_miles = serializers.FloatField(
        min_value=0.5, max_value=100, default=lambda: float(settings.FUEL_CORRIDOR_MILES)
    )
    initial_fuel_gallons = serializers.FloatField(min_value=0, allow_null=True, default=None)
    mpg = serializers.FloatField(
        min_value=1, max_value=100, default=lambda: float(settings.FUEL_MPG)
    )
    range_miles = serializers.FloatField(
        min_value=10, max_value=2000, default=lambda: float(settings.FUEL_RANGE_MILES)
    )
    min_stop_savings_usd = serializers.FloatField(min_value=0, max_value=1000, default=5.0)
    geometry = serializers.ChoiceField(choices=GEOMETRY_CHOICES, default="simplified")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        initial = attrs.get("initial_fuel_gallons")
        tank = attrs["range_miles"] / attrs["mpg"]
        if initial is not None and initial > tank + 1e-9:
            raise serializers.ValidationError(
                {
                    "initial_fuel_gallons": [
                        f"Ensure this value is less than or equal to the tank size "
                        f"({tank:g} gallons = range_miles / mpg)."
                    ]
                }
            )
        return attrs


# --------------------------------------------------------------------------- responses


def _miles(value: float) -> float:
    return round(value, 1)


def _gallons(value: float) -> float:
    return round(value, 2)


def _usd(value: float) -> float:
    return round(value, 2)


def _hours(value: float) -> float:
    return round(value, 2)


def _price(value: float) -> float:
    return round(value, 3)


def _deg(value: float) -> float:
    return round(value, 5)


def _ms(value: float) -> float:
    return round(value, 1)


def _location(query: str, location: Any) -> dict[str, Any]:
    return {
        "query": query,
        "label": location.label,
        "lat": _deg(location.lat),
        "lon": _deg(location.lon),
    }


def _stop(stop: Stop, result: TripResult) -> dict[str, Any]:
    station = result.stations.get(stop.station_id)
    place = station.place if station is not None else None
    return {
        "order": stop.order,
        "station": {
            "id": stop.station_id,
            "opis_id": station.opis_id if station else None,
            "name": station.name if station else None,
            "address": station.address if station else None,
            "city": station.city if station else None,
            "state": station.state if station else None,
        },
        "lat": _deg(place.lat) if place else None,
        "lon": _deg(place.lon) if place else None,
        "mile_marker": _miles(stop.position_miles),
        "detour_miles": _miles(stop.detour_miles),
        "price_per_gallon": _price(stop.price_per_gallon),
        "gallons_bought": _gallons(stop.gallons_bought),
        "cost_usd": _usd(stop.cost_usd),
        "fuel_on_arrival_gallons": _gallons(stop.fuel_on_arrival_gallons),
        "fuel_on_departure_gallons": _gallons(stop.fuel_on_departure_gallons),
    }


def _geometry(coordinates: Sequence[tuple[float, float]], mode: str) -> dict[str, Any] | None:
    if mode == "none":
        return None
    if mode == "simplified":
        coordinates = simplify(coordinates, DEFAULT_TOLERANCE_DEG)
    elif mode != "full":
        raise ValueError(f"unknown geometry mode {mode!r}")
    return {
        "type": "LineString",
        "coordinates": [[_deg(lon), _deg(lat)] for lon, lat in coordinates],
    }


def build_route_payload(
    result: TripResult, map_url: str, geometry: str = "simplified"
) -> dict[str, Any]:
    """The 200 response body of ``/api/route/`` (SPEC § API)."""
    plan, config, route = result.plan, result.config, result.route
    route_geometry = _geometry(route.coordinates, geometry)
    stops = [_stop(stop, result) for stop in plan.stops]
    # Totals are sums of the rounded per-stop values so the response is self-consistent
    # (sum(stops.cost_usd) == total_cost_usd exactly, not off by a cent).
    total_gallons = round(sum(s["gallons_bought"] for s in stops), 2)
    total_cost = round(sum(s["cost_usd"] for s in stops), 2)
    return {
        "start": _location(result.start_query, result.start),
        "finish": _location(result.finish_query, result.finish),
        "route": {
            "distance_miles": _miles(route.distance_miles),
            "duration_hours": _hours(route.duration_hours),
            "geometry": route_geometry,
            "provider": route.provider,
            "map_url": map_url,
        },
        "fuel": {
            "mpg": config.mpg,
            "range_miles": config.range_miles,
            "tank_gallons": _gallons(config.tank_gallons),
            "initial_fuel_gallons": _gallons(plan.initial_fuel_gallons),
            "total_gallons": total_gallons,
            "total_cost_usd": total_cost,
            "fuel_at_finish_gallons": _gallons(plan.fuel_at_finish_gallons),
            "min_stop_savings_usd": config.min_stop_savings_usd,
            "merged_stops": plan.merged_stops,
            "warnings": list(plan.warnings),
            "stops": stops,
        },
        "meta": {
            "external_calls": result.external_calls,
            "cached_route": route.cached,
            "candidate_stations": result.candidate_count,
            "geometry_points": len(route_geometry["coordinates"]) if route_geometry else 0,
            "routing_ms": _ms(result.routing_ms),
            "compute_ms": _ms(result.compute_ms),
        },
    }
