"""import_fuel_prices: CSV parsing, dedupe rule, fixture load, idempotency. No network."""

from __future__ import annotations

import json
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from fuel.models import FuelStation, Place
from fuel.services import gazetteer

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price"
ROWS = [
    '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333',
    '20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899',
    # same id, place and price, different name: dropped (first name kept)
    '20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899',
    # same id and place, different price: kept
    '20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.959',
    # padded city, lower-case state; no Place for it in the fixture
    "9,KWIK TRIP #796,I-94 EXIT 143,Tomah                    ,wi,420,3.28733333",
]
FIXTURE = [
    {"city": "Big Cabin", "state": "OK", "lat": 36.54, "lon": -95.22, "source": "geonames"},
    {"city": "Gila Bend", "state": "AZ", "lat": 32.95, "lon": -112.72, "source": "geonames"},
]


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "prices.csv"
    path.write_bytes(("\r\n".join([HEADER, *ROWS]) + "\r\n").encode())
    return path


@pytest.fixture
def fixture_path(tmp_path: Path) -> Path:
    path = tmp_path / "places.json"
    path.write_text(json.dumps(FIXTURE))
    return path


@pytest.fixture(autouse=True)
def _reset_station_index():
    gazetteer.reload_station_index()
    yield
    gazetteer.reload_station_index()


def run_import(csv_path: Path, fixture_path: Path) -> str:
    out = StringIO()
    call_command("import_fuel_prices", csv=csv_path, fixture=fixture_path, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_dedupe_keeps_first_name_and_distinct_prices(csv_path: Path, fixture_path: Path) -> None:
    output = run_import(csv_path, fixture_path)

    assert "Rows read: 5" in output
    assert "Duplicates dropped: 1" in output
    assert "Stations created: 4" in output
    assert "Stations without coordinates: 1" in output
    gila = FuelStation.objects.filter(opis_id=20).order_by("retail_price")
    assert [(s.name, s.retail_price) for s in gila] == [
        ("PILOT TRAVEL CENTER #1243", Decimal("3.89900")),
        ("PILOT #1243", Decimal("3.95900")),
    ]


@pytest.mark.django_db
def test_rows_are_cleaned_and_linked(csv_path: Path, fixture_path: Path) -> None:
    run_import(csv_path, fixture_path)

    tomah = FuelStation.objects.get(opis_id=9)
    assert (tomah.city, tomah.state, tomah.place) == ("Tomah", "WI", None)
    cabin = FuelStation.objects.get(opis_id=7)
    assert cabin.place == Place.objects.get(city="Big Cabin", state="OK")
    assert cabin.retail_price == Decimal("3.00733")
    assert cabin.rack_id == 307
    assert cabin.address == "I-44, EXIT 283 & US-69"


@pytest.mark.django_db
def test_import_is_idempotent(csv_path: Path, fixture_path: Path) -> None:
    run_import(csv_path, fixture_path)
    first = sorted(FuelStation.objects.values_list("opis_id", "name", "retail_price", "place"))
    run_import(csv_path, fixture_path)
    second = sorted(FuelStation.objects.values_list("opis_id", "name", "retail_price", "place"))

    assert FuelStation.objects.count() == 4
    assert Place.objects.count() == 2
    assert first == second


@pytest.mark.django_db
def test_fixture_load_upserts_places(csv_path: Path, fixture_path: Path) -> None:
    Place.objects.create(city="Big Cabin", state="OK", lat=0.0, lon=0.0, source="manual")
    Place.objects.create(city="Elsewhere", state="TX", lat=31.0, lon=-99.0, source="manual")

    output = run_import(csv_path, fixture_path)

    assert "Places loaded from fixture: 2" in output
    cabin = Place.objects.get(city="Big Cabin", state="OK")
    assert (cabin.lat, cabin.lon, cabin.source) == (36.54, -95.22, "geonames")
    assert Place.objects.filter(city="Elsewhere").exists()  # places are never deleted
    assert Place.objects.count() == 3


@pytest.mark.django_db
def test_import_without_fixture_leaves_stations_unplaced(csv_path: Path, tmp_path: Path) -> None:
    output = run_import(csv_path, tmp_path / "missing.json")

    assert "Stations without coordinates: 4" in output
    assert not FuelStation.objects.filter(place__isnull=False).exists()


@pytest.mark.django_db
def test_import_refreshes_station_index(csv_path: Path, fixture_path: Path) -> None:
    assert gazetteer.get_station_index().station_count == 0
    run_import(csv_path, fixture_path)
    assert gazetteer.get_station_index().station_count == 3
