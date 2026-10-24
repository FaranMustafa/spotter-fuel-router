"""Gazetteer: city normalization, GeoNames index, place lookup, station arrays. No network."""

from __future__ import annotations

import io
import zipfile
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from fuel.models import FuelStation, Place
from fuel.services import gazetteer
from fuel.services.gazetteer import GeoNamesIndex, normalize_city


def _row(place: str, state_name: str, code: str, lat: float | str, lon: float | str) -> list[str]:
    return [
        "US",
        "00000",
        place,
        state_name,
        code,
        "County",
        "001",
        "",
        "",
        str(lat),
        str(lon),
        "4",
    ]


SAMPLE_ROWS = [
    _row("Fort Worth", "Texas", "TX", 32.70, -97.30),
    _row("Fort Worth", "Texas", "TX", 32.80, -97.40),
    _row("Saint Louis", "Missouri", "MO", 38.63, -90.20),
    _row("Big Cabin", "Oklahoma", "OK", 36.54, -95.22),
    _row("Broken", "Oklahoma", "OK", "", ""),  # missing coordinates are skipped
]

COMPACT_ROWS = [
    _row("McLeansville", "North Carolina", "NC", 36.12, -79.67),
    _row("Deforest", "Wisconsin", "WI", 43.25, -89.34),
    _row("La Grange", "Texas", "TX", 29.90, -96.88),
    _row("Lagrange", "Texas", "TX", 30.00, -97.00),
]


@pytest.fixture
def sample_index() -> GeoNamesIndex:
    return GeoNamesIndex.from_rows(SAMPLE_ROWS)


@pytest.fixture(autouse=True)
def _isolated_caches(sample_index: GeoNamesIndex):
    gazetteer.set_geonames_index(sample_index)
    gazetteer.reload_station_index()
    yield
    gazetteer.set_geonames_index(None)
    gazetteer.reload_station_index()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ft. Worth", "fort worth"),
        ("Ft Worth", "fort worth"),
        ("St Louis", "saint louis"),
        ("St. Louis", "saint louis"),
        ("ST. LOUIS", "saint louis"),
        ("Mt. Vernon", "mount vernon"),
        ("Mt Pleasant", "mount pleasant"),
        ("Ste. Genevieve", "sainte genevieve"),
        ("  Big   Cabin  ", "big cabin"),
        ("O'Fallon", "ofallon"),
        ("Winston-Salem", "winston salem"),
        ("Coeur d'Alene", "coeur dalene"),
        ("Fort St John", "fort st john"),  # only a leading abbreviation is expanded
        ("St", "st"),  # a lone token is not treated as an abbreviation
        ("Stanton", "stanton"),
        ("", ""),
    ],
)
def test_normalize_city(raw: str, expected: str) -> None:
    assert normalize_city(raw) == expected


def test_geonames_index_averages_postal_codes(sample_index: GeoNamesIndex) -> None:
    assert len(sample_index) == 3
    assert sample_index.postal_code_count == 4
    lat, lon = sample_index.lookup("Ft. Worth", "tx")
    assert lat == pytest.approx(32.75)
    assert lon == pytest.approx(-97.35)
    assert sample_index.lookup("St. Louis", "MO") == pytest.approx((38.63, -90.20))
    assert sample_index.lookup("Fort Worth", "OK") is None
    assert sample_index.lookup("Broken", "OK") is None


def test_geonames_index_space_insensitive_fallback() -> None:
    index = GeoNamesIndex.from_rows(COMPACT_ROWS)
    assert index.lookup("Mc Leansville", "NC") == pytest.approx((36.12, -79.67))
    assert index.lookup("De Forest", "WI") == pytest.approx((43.25, -89.34))
    # exact keys still win; a compact key shared by two places is ambiguous
    assert index.lookup("La Grange", "TX") == pytest.approx((29.90, -96.88))
    assert index.lookup("Lagrange", "TX") == pytest.approx((30.00, -97.00))
    assert index.lookup("La grange", "TX") == pytest.approx((29.90, -96.88))
    assert index.lookup("L A Grange", "TX") is None


def test_geonames_index_state_names(sample_index: GeoNamesIndex) -> None:
    assert sample_index.state_name_to_code == {"Texas": "TX", "Missouri": "MO", "Oklahoma": "OK"}


def test_geonames_index_from_zip(tmp_path: Path) -> None:
    text = "\n".join("\t".join(row) for row in SAMPLE_ROWS) + "\n"
    path = tmp_path / "US.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("US.txt", text)
    index = GeoNamesIndex.from_zip(path)
    assert index.lookup("Big Cabin", "OK") == pytest.approx((36.54, -95.22))
    assert len(index) == 3


def test_geonames_index_from_text() -> None:
    text = "\t".join(_row("Tomah", "Wisconsin", "WI", 43.98, -90.50)) + "\n"
    index = GeoNamesIndex.from_text(io.StringIO(text))
    assert index.lookup("tomah", "WI") == pytest.approx((43.98, -90.50))


@pytest.mark.django_db
def test_lookup_place_prefers_place_table() -> None:
    saved = Place.objects.create(city="Fort Worth", state="TX", lat=1.0, lon=2.0, source="manual")
    assert gazetteer.lookup_place("fort worth", "tx") == saved
    assert gazetteer.lookup_place("Ft. Worth", "TX") == saved  # normalized match in state


@pytest.mark.django_db
def test_lookup_place_falls_back_to_geonames_unsaved() -> None:
    place = gazetteer.lookup_place("St Louis", "MO")
    assert place is not None
    assert place.pk is None
    assert place.source == Place.Source.GEONAMES
    assert (place.lat, place.lon) == pytest.approx((38.63, -90.20))
    assert Place.objects.count() == 0


@pytest.mark.django_db
def test_lookup_place_miss() -> None:
    assert gazetteer.lookup_place("Nowhere", "TX") is None
    assert gazetteer.lookup_place("Fort Worth", "Texas") is None  # needs a 2-letter code


def _station(opis_id: int, price: str, place: Place | None) -> FuelStation:
    return FuelStation.objects.create(
        opis_id=opis_id,
        name=f"Stop {opis_id}",
        address="I-1, EXIT 1",
        city=place.city if place else "Nowhere",
        state=place.state if place else "TX",
        rack_id=1,
        retail_price=Decimal(price),
        place=place,
    )


@pytest.mark.django_db
def test_station_index_arrays_exclude_unplaced_stations() -> None:
    cabin = Place.objects.create(
        city="Big Cabin", state="OK", lat=36.54, lon=-95.22, source="geonames"
    )
    tomah = Place.objects.create(city="Tomah", state="WI", lat=43.98, lon=-90.5, source="geonames")
    a = _station(7, "3.00733", cabin)
    b = _station(9, "3.28733", tomah)
    _station(11, "3.50000", None)

    index = gazetteer.get_station_index()
    assert index.station_count == 2
    for array in (index.ids, index.lats, index.lons, index.prices):
        assert array.shape == (2,)
    assert index.ids.dtype == np.int64
    assert index.lats.dtype == index.lons.dtype == index.prices.dtype == np.float64
    assert index.ids.tolist() == [a.pk, b.pk]
    assert index.lats.tolist() == pytest.approx([36.54, 43.98])
    assert index.lons.tolist() == pytest.approx([-95.22, -90.5])
    assert index.prices.tolist() == pytest.approx([3.00733, 3.28733])


@pytest.mark.django_db
def test_station_index_is_cached_until_reload() -> None:
    assert gazetteer.get_station_index().station_count == 0
    place = Place.objects.create(city="Tomah", state="WI", lat=43.98, lon=-90.5, source="geonames")
    _station(9, "3.28733", place)
    assert gazetteer.get_station_index() is gazetteer.get_station_index()
    assert gazetteer.get_station_index().station_count == 0
    gazetteer.reload_station_index()
    assert gazetteer.get_station_index().station_count == 1


@pytest.mark.django_db
def test_station_index_empty_database() -> None:
    index = gazetteer.get_station_index()
    assert index.station_count == 0
    assert index.ids.shape == (0,)
    assert index.ids.dtype == np.int64
    assert index.prices.dtype == np.float64
