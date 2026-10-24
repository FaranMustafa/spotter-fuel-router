"""Tests for the trip orchestration service. OSRM is mocked with `responses`.

The helpers and fixtures here are also imported by ``tests/test_api.py``.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

import pytest
import responses
from django.core.cache import cache

from fuel.models import FuelStation, Place
from fuel.services import gazetteer
from fuel.services.corridor import MILES_PER_DEGREE, route_length_miles
from fuel.services.geocoding import GeocodingError
from fuel.services.planner import UnreachableError
from fuel.services.trip import TripResult, plan_trip

OSRM_URL = re.compile(r"https://osrm\.test/route/v1/driving/.*")
METERS_PER_MILE = 1609.344

# A straight east-west test route along latitude 35 N, starting at 110 W.
ROUTE_LAT = 35.0
ROUTE_LON0 = -110.0
MILES_PER_LON_DEGREE = MILES_PER_DEGREE * math.cos(math.radians(ROUTE_LAT))
LONG_ROUTE_MILES = 1200.0
SHORT_ROUTE_MILES = 200.0

# (mile along the route, price per gallon)
HAPPY_STATIONS = [
    (50, "3.50"),
    (200, "3.20"),
    (420, "3.90"),
    (480, "3.10"),
    (700, "3.60"),
    (850, "2.95"),
    (1000, "3.40"),
    (1150, "3.80"),
]


def lon_at(mile: float) -> float:
    return ROUTE_LON0 + mile / MILES_PER_LON_DEGREE


def route_coords(miles: float, vertices: int = 50) -> list[list[float]]:
    """``vertices`` evenly spaced (lon, lat) points covering ``miles`` along the route."""
    return [[lon_at(miles * i / (vertices - 1)), ROUTE_LAT] for i in range(vertices)]


def osrm_payload(coords: list[list[float]], distance_factor: float = 1.0) -> dict:
    distance_miles = route_length_miles([tuple(c) for c in coords]) * distance_factor
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_miles * METERS_PER_MILE,
                "duration": distance_miles / 60.0 * 3600.0,  # 60 mph
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        ],
    }


def mock_osrm(miles: float = LONG_ROUTE_MILES, distance_factor: float = 1.0) -> None:
    responses.get(OSRM_URL, json=osrm_payload(route_coords(miles), distance_factor))


def make_place(city: str, mile: float, state: str = "AZ", lat_offset: float = 0.0) -> Place:
    return Place.objects.create(
        city=city, state=state, lat=ROUTE_LAT + lat_offset, lon=lon_at(mile), source="manual"
    )


def make_stations(stations: list[tuple[float, str]]) -> list[FuelStation]:
    created = []
    for n, (mile, price) in enumerate(stations, start=1):
        place = make_place(f"Town {mile}", mile)
        created.append(
            FuelStation.objects.create(
                opis_id=1000 + n,
                name=f"Truck Stop {mile}",
                address=f"I-40, EXIT {n}",
                city=place.city,
                state=place.state,
                rack_id=1,
                retail_price=Decimal(price),
                place=place,
            )
        )
    gazetteer.reload_station_index()
    return created


@pytest.fixture(autouse=True)
def _isolate(settings):
    settings.OSRM_BASE_URL = "https://osrm.test"
    settings.FUEL_MPG = 10
    settings.FUEL_RANGE_MILES = 500
    settings.FUEL_CORRIDOR_MILES = 10
    cache.clear()
    gazetteer.set_geonames_index(gazetteer.GeoNamesIndex({}, {}))
    gazetteer.reload_station_index()
    yield
    cache.clear()
    gazetteer.set_geonames_index(None)
    gazetteer.reload_station_index()


@pytest.fixture
def endpoints(db):
    """Start/finish places on the test route (resolved through the Place table)."""
    make_place("Startville", 0)
    make_place("Endburg", LONG_ROUTE_MILES, state="TN")
    make_place("Shortend", SHORT_ROUTE_MILES, state="NM")


@pytest.fixture
def happy_stations(db):
    return make_stations(HAPPY_STATIONS)


@pytest.mark.django_db
@responses.activate
def test_plan_trip_happy_path(endpoints, happy_stations):
    mock_osrm()

    result = plan_trip("Startville, AZ", "Endburg, TN")

    assert isinstance(result, TripResult)
    assert result.start.label == "Startville, AZ"
    assert result.finish.label == "Endburg, TN"
    assert result.external_calls == 1
    assert result.candidate_count == len(HAPPY_STATIONS)
    assert result.routing_ms >= 0 and result.compute_ms >= 0
    assert result.corridor_miles == 10

    stops = result.plan.stops
    assert [round(s.position_miles) for s in stops] == [480, 850]
    assert [s.price_per_gallon for s in stops] == [3.10, 2.95]
    # The test route is ~1,200 miles (a constant-latitude line, not a great circle).
    assert result.plan.total_gallons == pytest.approx((LONG_ROUTE_MILES - 500) / 10, abs=0.01)
    # Every stop's station is in the bulk-loaded mapping with its place.
    assert set(result.stations) == {s.station_id for s in stops}
    for stop in stops:
        station = result.stations[stop.station_id]
        assert station.place.lon == pytest.approx(lon_at(round(stop.position_miles)))


@pytest.mark.django_db
@responses.activate
def test_candidate_positions_are_scaled_to_osrm_distance(endpoints, happy_stations):
    mock_osrm(distance_factor=1.05)

    result = plan_trip("Startville, AZ", "Endburg, TN")

    assert result.route.distance_miles == pytest.approx(LONG_ROUTE_MILES * 1.05, rel=1e-3)
    assert result.plan.route_miles == pytest.approx(result.route.distance_miles)
    # 480 polyline miles * 1.05 = 504 OSRM miles: beyond the initial range, so the
    # planner must pick another first stop; every stop lies on the scaled mile scale.
    scaled = [m * 1.05 for m, _ in HAPPY_STATIONS]
    for stop in result.plan.stops:
        assert min(abs(stop.position_miles - m) for m in scaled) < 0.1
    assert all(s.position_miles <= 500 for s in result.plan.stops[:1])
    assert result.plan.total_gallons == pytest.approx(
        (result.route.distance_miles - 500) / 10, rel=1e-6
    )


@pytest.mark.django_db
@responses.activate
def test_plan_trip_unreachable_raises(endpoints):
    make_stations([(50, "3.50"), (200, "3.20")])
    mock_osrm()

    with pytest.raises(UnreachableError) as excinfo:
        plan_trip("Startville, AZ", "Endburg, TN")

    assert excinfo.value.from_miles == pytest.approx(200, abs=0.01)
    assert excinfo.value.to_finish is True


@pytest.mark.django_db
@responses.activate
def test_plan_trip_overrides_and_cache(endpoints, happy_stations):
    mock_osrm()

    first = plan_trip("Startville, AZ", "Endburg, TN", initial_fuel_gallons=10)
    second = plan_trip("35.0,-110.0", "Endburg, TN", mpg=8, range_miles=600, corridor_miles=5)

    assert first.plan.initial_fuel_gallons == 10
    assert first.external_calls == 1
    assert second.route.cached is True
    assert second.external_calls == 0
    assert second.config.tank_gallons == 75
    assert second.corridor_miles == 5
    assert len(responses.calls) == 1


@pytest.mark.django_db
def test_plan_trip_propagates_geocoding_errors(endpoints):
    with pytest.raises(GeocodingError) as excinfo:
        plan_trip("51.5,-0.12", "Endburg, TN")
    assert excinfo.value.code == "outside_usa"
