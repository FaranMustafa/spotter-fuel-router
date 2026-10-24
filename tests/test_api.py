"""API and map-page tests for ``/api/route/`` with OSRM mocked by `responses`."""

from __future__ import annotations

import json

import pytest
import responses
from django.urls import reverse

from tests import test_trip
from tests.test_trip import HAPPY_STATIONS, OSRM_URL, make_stations, mock_osrm

# Shared fixtures from test_trip (pytest resolves fixtures by name in this module).
_isolate = test_trip._isolate
endpoints = test_trip.endpoints
happy_stations = test_trip.happy_stations

ROUTE_URL = "/api/route/"
MAP_URL = "/api/route/map/"
TRIP = {"start": "Startville, AZ", "finish": "Endburg, TN"}


def _decimals(value: float) -> int:
    text = repr(value)
    return len(text.split(".")[1]) if "." in text else 0


@pytest.mark.django_db
@responses.activate
def test_route_happy_path_shape_and_rounding(client, endpoints, happy_stations):
    mock_osrm()

    response = client.get(ROUTE_URL, TRIP)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"start", "finish", "route", "fuel", "meta"}
    assert body["start"] == {
        "query": "Startville, AZ",
        "label": "Startville, AZ",
        "lat": 35.0,
        "lon": -110.0,
    }
    assert body["finish"]["label"] == "Endburg, TN"

    route = body["route"]
    assert set(route) == {"distance_miles", "duration_hours", "geometry", "provider", "map_url"}
    assert route["distance_miles"] == pytest.approx(1200, abs=0.1)
    assert route["duration_hours"] == pytest.approx(20, abs=0.01)
    assert route["provider"] == "osrm"
    assert route["geometry"]["type"] == "LineString"
    assert len(route["geometry"]["coordinates"]) == 50
    lon, lat = route["geometry"]["coordinates"][0]
    assert (lon, lat) == (-110.0, 35.0)
    assert route["map_url"].startswith("http://testserver/api/route/map/?")
    assert "start=Startville%2C+AZ" in route["map_url"]

    fuel = body["fuel"]
    assert fuel["mpg"] == 10
    assert fuel["range_miles"] == 500
    assert fuel["tank_gallons"] == 50
    assert fuel["initial_fuel_gallons"] == 50
    assert fuel["warnings"] == []
    stops = fuel["stops"]
    assert len(stops) >= 2
    assert [s["order"] for s in stops] == list(range(1, len(stops) + 1))
    assert [s["mile_marker"] for s in stops] == [480.0, 850.0]
    assert [s["price_per_gallon"] for s in stops] == [3.1, 2.95]
    first = stops[0]
    assert set(first) == {
        "order",
        "station",
        "lat",
        "lon",
        "mile_marker",
        "detour_miles",
        "price_per_gallon",
        "gallons_bought",
        "cost_usd",
        "fuel_on_arrival_gallons",
        "fuel_on_departure_gallons",
    }
    assert set(first["station"]) == {"id", "opis_id", "name", "address", "city", "state"}
    assert first["station"]["name"] == "Truck Stop 480"
    assert first["station"]["city"] == "Town 480"
    for stop in stops:
        assert _decimals(stop["mile_marker"]) <= 1
        assert _decimals(stop["detour_miles"]) <= 1
        assert _decimals(stop["gallons_bought"]) <= 2
        assert _decimals(stop["cost_usd"]) <= 2
        assert _decimals(stop["price_per_gallon"]) <= 3
        assert _decimals(stop["lat"]) <= 5 and _decimals(stop["lon"]) <= 5
        assert stop["fuel_on_departure_gallons"] <= 50
    assert _decimals(route["distance_miles"]) <= 1
    assert _decimals(route["duration_hours"]) <= 2
    # Totals equal the sum of the (rounded) stops.
    assert fuel["total_gallons"] == round(sum(s["gallons_bought"] for s in stops), 2)
    assert fuel["total_cost_usd"] == round(sum(s["cost_usd"] for s in stops), 2)
    assert fuel["total_gallons"] == pytest.approx(70, abs=0.01)
    assert fuel["fuel_at_finish_gallons"] == pytest.approx(0, abs=0.01)

    meta = body["meta"]
    assert meta["external_calls"] == 1
    assert meta["cached_route"] is False
    assert meta["candidate_stations"] == len(HAPPY_STATIONS)
    assert meta["routing_ms"] >= 0 and meta["compute_ms"] >= 0


@pytest.mark.django_db
@responses.activate
def test_route_short_trip_has_no_stops_and_a_warning(client, endpoints, happy_stations):
    mock_osrm(miles=200)

    response = client.get(ROUTE_URL, {"start": "Startville, AZ", "finish": "Shortend, NM"})

    assert response.status_code == 200
    fuel = response.json()["fuel"]
    assert fuel["stops"] == []
    assert fuel["total_cost_usd"] == 0
    assert fuel["total_gallons"] == 0
    assert fuel["fuel_at_finish_gallons"] == pytest.approx(30, abs=0.01)
    assert len(fuel["warnings"]) == 1


@pytest.mark.django_db
@responses.activate
def test_route_unreachable_returns_422(client, endpoints):
    make_stations([(50, "3.50"), (200, "3.20")])
    mock_osrm()

    response = client.get(ROUTE_URL, TRIP)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unreachable"
    assert "No fuel station within 500 miles" in error["message"]
    assert error["details"] == {
        "from_miles": pytest.approx(200, abs=0.1),
        "to_miles": pytest.approx(1200, abs=0.1),
        "range_miles": 500,
        "suggestion": "Increase corridor_miles or range_miles",
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"start": "Startville, AZ"}, "finish"),
        ({**TRIP, "corridor_miles": 0}, "corridor_miles"),
        ({**TRIP, "mpg": "abc"}, "mpg"),
        ({**TRIP, "initial_fuel_gallons": 60}, "initial_fuel_gallons"),
        ({**TRIP, "start": "x" * 201}, "start"),
    ],
)
def test_route_invalid_input_returns_400(client, params, field):
    response = client.get(ROUTE_URL, params)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert field in error["details"]
    assert isinstance(error["details"][field], list) and error["details"][field]
    assert error["message"]


@pytest.mark.django_db
def test_route_outside_usa_returns_400(client, endpoints):
    response = client.get(ROUTE_URL, {"start": "51.5,-0.12", "finish": "Endburg, TN"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "outside_usa"


@pytest.mark.django_db
@responses.activate
def test_route_no_route_returns_422(client, endpoints):
    responses.get(OSRM_URL, json={"code": "NoRoute", "message": "Impossible route"}, status=400)

    response = client.get(ROUTE_URL, TRIP)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_route"


@pytest.mark.django_db
@responses.activate
def test_route_provider_down_returns_502(client, endpoints):
    responses.get(OSRM_URL, body="Bad gateway", status=503)

    response = client.get(ROUTE_URL, TRIP)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "routing_unavailable"


@pytest.mark.django_db
@responses.activate
def test_route_accepts_lat_lon_inputs(client, endpoints, happy_stations):
    mock_osrm()

    response = client.get(ROUTE_URL, {"start": "35.0,-110.0", "finish": "Endburg, TN"})

    assert response.status_code == 200
    body = response.json()
    assert body["start"]["query"] == "35.0,-110.0"
    assert body["start"]["label"] == "35.0000, -110.0000"
    assert (body["start"]["lat"], body["start"]["lon"]) == (35.0, -110.0)
    assert len(body["fuel"]["stops"]) == 2


@pytest.mark.django_db
@responses.activate
def test_route_post_json(client, endpoints, happy_stations):
    mock_osrm()

    response = client.post(
        ROUTE_URL,
        data=json.dumps({**TRIP, "initial_fuel_gallons": 25, "corridor_miles": 20}),
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fuel"]["initial_fuel_gallons"] == 25
    assert body["fuel"]["total_gallons"] == pytest.approx(95, abs=0.01)
    assert "corridor_miles=20" in body["route"]["map_url"]
    assert "initial_fuel_gallons=25" in body["route"]["map_url"]


@pytest.mark.django_db
def test_route_post_malformed_json_returns_400(client):
    response = client.post(ROUTE_URL, data="{not json", content_type="application/json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "parse_error"


@pytest.mark.django_db
@responses.activate
def test_route_cached_second_call_makes_no_external_calls(client, endpoints, happy_stations):
    mock_osrm()

    first = client.get(ROUTE_URL, TRIP).json()
    second = client.get(ROUTE_URL, TRIP).json()

    assert first["meta"]["external_calls"] == 1
    assert first["meta"]["cached_route"] is False
    assert second["meta"]["external_calls"] == 0
    assert second["meta"]["cached_route"] is True
    assert second["fuel"] == first["fuel"]
    assert len(responses.calls) == 1


@pytest.mark.django_db
@responses.activate
def test_map_view_renders_route(client, endpoints, happy_stations):
    mock_osrm()

    response = client.get(MAP_URL, TRIP)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    html = response.content.decode()
    assert "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" in html
    assert "2 fuel stops" in html
    assert "Truck Stop 480" in html and "Truck Stop 850" in html
    assert 'id="route-data"' in html
    payload = response.context["payload"]
    assert len(payload["fuel"]["stops"]) == 2
    assert payload["route"]["geometry"]["type"] == "LineString"


def test_map_url_reverses():
    assert reverse("fuel_api:route-map") == MAP_URL
    assert reverse("fuel_api:route") == ROUTE_URL


@pytest.mark.django_db
@responses.activate
def test_map_view_renders_unreachable_error(client, endpoints):
    make_stations([(50, "3.50"), (200, "3.20")])
    mock_osrm()

    response = client.get(MAP_URL, TRIP)

    assert response.status_code == 422
    html = response.content.decode()
    assert "Route could not be planned" in html
    assert "No fuel station within 500 miles" in html
    assert "Increase corridor_miles or range_miles" in html
    assert response.context["payload"] is None


@pytest.mark.django_db
def test_map_view_renders_invalid_input(client):
    response = client.get(MAP_URL, {"start": "Startville, AZ"})

    assert response.status_code == 400
    html = response.content.decode()
    assert "invalid_request" in html
    assert "finish" in html
