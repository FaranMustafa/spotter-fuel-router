"""Tests for the OSRM routing client. All HTTP is mocked with `responses`."""

from __future__ import annotations

import re

import pytest
import requests
import responses
from django.core.cache import cache

from fuel.services.http import get_external_calls, reset_external_calls
from fuel.services.routing import Route, RoutingError, get_route

OSRM_URL = re.compile(r"https://osrm\.test/route/v1/driving/.*")
START = (32.7767, -96.7970)  # Dallas (lat, lon)
FINISH = (29.7604, -95.3698)  # Houston


@pytest.fixture(autouse=True)
def _setup(settings):
    settings.OSRM_BASE_URL = "https://osrm.test"
    cache.clear()
    reset_external_calls()
    yield
    cache.clear()


def _ok_payload(distance_m: float = 386_242.0, duration_s: float = 13_500.0) -> dict:
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_m,
                "duration": duration_s,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-96.797, 32.7767], [-96.0, 31.5], [-95.3698, 29.7604]],
                },
            }
        ],
    }


@responses.activate
def test_success_converts_units_and_builds_request():
    responses.get(OSRM_URL, json=_ok_payload())

    route = get_route(START, FINISH)

    assert isinstance(route, Route)
    assert route.distance_miles == pytest.approx(386_242.0 / 1609.344)
    assert route.duration_hours == pytest.approx(13_500.0 / 3600)
    assert route.coordinates == ((-96.797, 32.7767), (-96.0, 31.5), (-95.3698, 29.7604))
    assert isinstance(route.coordinates, tuple)
    assert route.provider == "osrm"
    assert route.cached is False

    url = responses.calls[0].request.url
    assert "/route/v1/driving/-96.797,32.7767;-95.3698,29.7604" in url
    for part in ("overview=full", "geometries=geojson", "steps=false", "annotations=false"):
        assert part in url
    assert get_external_calls() == 1


@responses.activate
def test_second_identical_call_hits_cache():
    responses.get(OSRM_URL, json=_ok_payload())

    first = get_route(START, FINISH)
    second = get_route(START, FINISH)

    responses.assert_call_count(responses.calls[0].request.url, 1)
    assert len(responses.calls) == 1
    assert first.cached is False
    assert second.cached is True
    assert second.distance_miles == first.distance_miles
    assert second.coordinates == first.coordinates
    assert get_external_calls() == 1


@responses.activate
def test_cache_key_rounds_to_four_decimals():
    responses.get(OSRM_URL, json=_ok_payload())

    get_route(START, FINISH)
    again = get_route((32.77670001, -96.79700004), FINISH)

    assert again.cached is True
    assert len(responses.calls) == 1


@responses.activate
def test_no_route_raises_with_osrm_code():
    responses.get(
        OSRM_URL, status=400, json={"code": "NoRoute", "message": "Impossible route between points"}
    )

    with pytest.raises(RoutingError) as excinfo:
        get_route(START, FINISH)

    assert excinfo.value.code == "NoRoute"
    assert "Impossible route" in excinfo.value.message


@responses.activate
def test_no_route_with_200_status_also_raises():
    responses.get(OSRM_URL, json={"code": "NoRoute", "message": "No route found"})

    with pytest.raises(RoutingError) as excinfo:
        get_route(START, FINISH)

    assert excinfo.value.code == "NoRoute"


@responses.activate
def test_errors_are_not_cached():
    responses.get(OSRM_URL, json={"code": "NoRoute", "message": "nope"})
    responses.get(OSRM_URL, json=_ok_payload())

    with pytest.raises(RoutingError):
        get_route(START, FINISH)
    assert get_route(START, FINISH).cached is False


@responses.activate
def test_timeout_retries_once_then_raises():
    responses.get(OSRM_URL, body=requests.Timeout("timed out"))
    responses.get(OSRM_URL, body=requests.Timeout("timed out"))

    with pytest.raises(RoutingError) as excinfo:
        get_route(START, FINISH)

    assert excinfo.value.code == "timeout"
    assert len(responses.calls) == 2
    assert get_external_calls() == 2


@responses.activate
def test_connection_error_then_success_recovers():
    responses.get(OSRM_URL, body=requests.ConnectionError("reset"))
    responses.get(OSRM_URL, json=_ok_payload())

    route = get_route(START, FINISH)

    assert route.cached is False
    assert len(responses.calls) == 2


@responses.activate
def test_malformed_json_is_invalid_response():
    responses.get(OSRM_URL, body="<html>not json</html>", content_type="text/html")

    with pytest.raises(RoutingError) as excinfo:
        get_route(START, FINISH)

    assert excinfo.value.code == "invalid_response"


@responses.activate
def test_ok_without_routes_is_invalid_response():
    responses.get(OSRM_URL, json={"code": "Ok", "routes": []})

    with pytest.raises(RoutingError) as excinfo:
        get_route(START, FINISH)

    assert excinfo.value.code == "invalid_response"


@responses.activate
def test_server_error_without_json_is_http_error():
    responses.get(OSRM_URL, status=502, body="Bad Gateway")

    with pytest.raises(RoutingError) as excinfo:
        get_route(START, FINISH)

    assert excinfo.value.code == "http_error"
