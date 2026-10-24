"""Driving route lookup via OSRM, memoised in the Django cache."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from fuel.services.http import build_session, record_external_call

METERS_PER_MILE = 1609.344
SECONDS_PER_HOUR = 3600.0
CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_PREFIX = "route:v1:"


@dataclass(frozen=True)
class Route:
    distance_miles: float
    duration_hours: float
    coordinates: tuple[tuple[float, float], ...]  # (lon, lat), GeoJSON order
    provider: str = "osrm"
    cached: bool = False


class RoutingError(Exception):
    """Routing failed. ``code`` is OSRM's code or one of timeout/http_error/invalid_response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _cache_key(start: tuple[float, float], finish: tuple[float, float]) -> str:
    parts = (f"{value:.4f}" for value in (*start, *finish))
    return CACHE_PREFIX + ",".join(parts)


def _fetch(url: str, params: dict[str, str]) -> requests.Response:
    """GET with one retry on connection errors and timeouts."""
    session = build_session()
    last_error: requests.RequestException | None = None
    for _attempt in range(2):
        record_external_call()
        try:
            return session.get(url, params=params, timeout=settings.HTTP_TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
        except requests.RequestException as exc:
            raise RoutingError("http_error", f"Routing request failed: {exc}") from exc
    if isinstance(last_error, requests.Timeout):
        raise RoutingError("timeout", "The routing service did not respond in time.")
    raise RoutingError("http_error", f"Could not reach the routing service: {last_error}")


def _parse(payload: Any) -> Route:
    try:
        route = payload["routes"][0]
        distance_m = float(route["distance"])
        duration_s = float(route["duration"])
        coords = tuple((float(lon), float(lat)) for lon, lat in route["geometry"]["coordinates"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RoutingError(
            "invalid_response", "The routing service returned an unexpected response."
        ) from exc
    if len(coords) < 2:
        raise RoutingError("invalid_response", "The routing service returned an empty route.")
    return Route(
        distance_miles=distance_m / METERS_PER_MILE,
        duration_hours=duration_s / SECONDS_PER_HOUR,
        coordinates=coords,
    )


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> Route:
    """Return the driving route between two (lat, lon) points."""
    key = _cache_key(start, finish)
    hit = cache.get(key)
    if hit is not None:
        return dataclasses.replace(hit, cached=True)

    (lat1, lon1), (lat2, lon2) = start, finish
    url = f"{settings.OSRM_BASE_URL.rstrip('/')}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
        "annotations": "false",
    }
    response = _fetch(url, params)

    try:
        payload = response.json()
    except ValueError:
        payload = None

    osrm_code = payload.get("code") if isinstance(payload, dict) else None
    if osrm_code and osrm_code != "Ok":
        message = payload.get("message") or f"No route could be found ({osrm_code})."
        raise RoutingError(str(osrm_code), str(message))
    if response.status_code != 200:
        raise RoutingError(
            "http_error", f"The routing service returned HTTP {response.status_code}."
        )
    if payload is None or osrm_code != "Ok":
        raise RoutingError(
            "invalid_response", "The routing service returned an unexpected response."
        )

    route = _parse(payload)
    cache.set(key, route, CACHE_TTL_SECONDS)
    return route
