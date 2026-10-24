"""Tests for location resolution. Nominatim is mocked with `responses`."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import responses
from django.core.cache import cache

from fuel.services import geocoding
from fuel.services.geocoding import (
    STATE_NAMES,
    GeocodingError,
    Location,
    assert_inside_usa,
    resolve_location,
)
from fuel.services.http import get_external_calls, reset_external_calls

NOMINATIM_URL = "https://nominatim.test/search"


@dataclass(frozen=True)
class FakePlace:
    lat: float
    lon: float
    city: str
    state: str


GAZETTEER = {
    ("dallas", "TX"): FakePlace(32.7767, -96.7970, "Dallas", "TX"),
    ("salt lake city", "UT"): FakePlace(40.7608, -111.8910, "Salt Lake City", "UT"),
    ("charleston", "WV"): FakePlace(38.3498, -81.6326, "Charleston", "WV"),
}


@pytest.fixture(autouse=True)
def _setup(settings, monkeypatch):
    settings.NOMINATIM_BASE_URL = "https://nominatim.test"
    settings.NOMINATIM_USER_AGENT = "spotter-test/1.0"
    cache.clear()
    reset_external_calls()
    calls: list[tuple[str, str]] = []

    def fake_lookup(city: str, state: str):
        calls.append((city, state))
        return GAZETTEER.get((city.lower(), state))

    monkeypatch.setattr(geocoding, "_gazetteer_lookup", fake_lookup)
    yield calls
    cache.clear()


def _nominatim_result(**overrides) -> dict:
    result = {
        "lat": "35.4676",
        "lon": "-97.5164",
        "name": "Oklahoma City",
        "display_name": "Oklahoma City, Oklahoma County, Oklahoma, United States",
        "address": {
            "city": "Oklahoma City",
            "state": "Oklahoma",
            "ISO3166-2-lvl4": "US-OK",
            "country_code": "us",
        },
    }
    result.update(overrides)
    return result


# Coordinates


@pytest.mark.parametrize("text", ["32.7767,-96.797", "32.7767, -96.797", "  32.7767 ,  -96.797 "])
def test_coordinates_formats(text):
    loc = resolve_location(text)
    assert loc == Location(32.7767, -96.797, "32.7767, -96.7970", "coordinates")


@pytest.mark.parametrize("text", ["91,-96", "-91,-96", "40,181", "40,-180.5"])
def test_coordinate_range_validation(text):
    with pytest.raises(GeocodingError) as excinfo:
        resolve_location(text)
    assert excinfo.value.code == "invalid_input"


def test_coordinates_outside_usa_rejected():
    with pytest.raises(GeocodingError) as excinfo:
        resolve_location("51.5,-0.1")
    assert excinfo.value.code == "outside_usa"


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(39.0, -98.0), (61.2, -149.9), (21.3, -157.8)],  # Kansas, Anchorage, Honolulu
)
def test_assert_inside_usa_accepts_all_boxes(lat, lon):
    assert_inside_usa(lat, lon)


@pytest.mark.parametrize(("lat", "lon"), [(53.5, -113.5), (19.4, -99.1), (0.0, 0.0)])
def test_assert_inside_usa_rejects(lat, lon):
    with pytest.raises(GeocodingError) as excinfo:
        assert_inside_usa(lat, lon)
    assert excinfo.value.code == "outside_usa"


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_input_rejected(text):
    with pytest.raises(GeocodingError) as excinfo:
        resolve_location(text)
    assert excinfo.value.code == "invalid_input"


# Gazetteer


@pytest.mark.parametrize(
    "text",
    [
        "Dallas, TX",
        "Dallas TX",
        "dallas, tx",
        "  DALLAS ,   TX  ",
        "Dallas, Texas",
        "dallas,   texas",
        "Dallas Texas",
    ],
)
def test_city_state_formats_hit_gazetteer(text, _setup):
    loc = resolve_location(text)
    assert loc == Location(32.7767, -96.797, "Dallas, TX", "gazetteer")
    city, state = _setup[-1]
    assert (city.lower(), state) == ("dallas", "TX")
    assert get_external_calls() == 0


def test_multi_word_city_and_state():
    assert resolve_location("Salt Lake City, Utah").label == "Salt Lake City, UT"
    assert resolve_location("Charleston, West Virginia").label == "Charleston, WV"
    assert resolve_location("Charleston West Virginia").label == "Charleston, WV"


def test_state_names_cover_50_states_and_dc():
    assert len(STATE_NAMES) == 51
    assert len(set(STATE_NAMES.values())) == 51
    assert STATE_NAMES["district of columbia"] == "DC"


def test_gazetteer_import_fallback_returns_none(monkeypatch):
    monkeypatch.undo()
    # Whatever the state of the real gazetteer module, the function must not raise
    # ImportError; with the module absent it returns None.
    import sys

    monkeypatch.setitem(sys.modules, "fuel.services.gazetteer", None)
    assert geocoding._gazetteer_lookup("Dallas", "TX") is None


# Nominatim


@responses.activate
def test_nominatim_fallback_on_gazetteer_miss():
    responses.get(NOMINATIM_URL, json=[_nominatim_result()])

    loc = resolve_location("Oklahoma City, OK")

    assert loc == Location(35.4676, -97.5164, "Oklahoma City, OK", "nominatim")
    request = responses.calls[0].request
    assert request.headers["User-Agent"] == "spotter-test/1.0"
    for part in ("format=jsonv2", "limit=1", "addressdetails=1"):
        assert part in request.url
    assert "countrycodes" not in request.url
    assert "q=Oklahoma+City%2C+OK" in request.url
    assert get_external_calls() == 1


@responses.activate
def test_unparsed_text_goes_to_nominatim():
    responses.get(NOMINATIM_URL, json=[_nominatim_result()])

    loc = resolve_location("Oklahoma City")

    assert loc.source == "nominatim"
    assert "q=Oklahoma+City" in responses.calls[0].request.url


@responses.activate
def test_nominatim_label_falls_back_to_display_name():
    responses.get(
        NOMINATIM_URL,
        json=[
            _nominatim_result(
                display_name="Grand Canyon Village, Coconino County, Arizona, United States",
                address={"country_code": "us"},
                lat="36.05",
                lon="-112.14",
            )
        ],
    )
    assert resolve_location("Grand Canyon").label == "Grand Canyon Village, Coconino County"


@responses.activate
def test_nominatim_not_found():
    responses.get(NOMINATIM_URL, json=[])

    with pytest.raises(GeocodingError) as excinfo:
        resolve_location("Nowhereville, KS")
    assert excinfo.value.code == "not_found"
    assert "countrycodes" not in responses.calls[0].request.url


@pytest.mark.parametrize("text", ["Toronto, ON", "Toronto ON", "Montreal, qc", "Calgary, AB"])
@responses.activate
def test_canadian_province_rejected_without_network(text, _setup):
    with pytest.raises(GeocodingError) as excinfo:
        resolve_location(text)
    assert excinfo.value.code == "outside_usa"
    assert "Canada" in excinfo.value.message
    assert len(responses.calls) == 0
    assert get_external_calls() == 0
    assert _setup == []  # gazetteer not consulted either


@responses.activate
def test_nominatim_canadian_best_match_rejected():
    responses.get(
        NOMINATIM_URL,
        json=[
            _nominatim_result(
                lat="43.65",
                lon="-79.38",
                display_name="Toronto, Ontario, Canada",
                address={"city": "Toronto", "country": "Canada", "country_code": "ca"},
            )
        ],
    )

    with pytest.raises(GeocodingError) as excinfo:
        resolve_location("Toronto")
    assert excinfo.value.code == "outside_usa"
    assert excinfo.value.message == "'Toronto' resolves to Canada, not the USA."
    assert "countrycodes" not in responses.calls[0].request.url


@responses.activate
def test_nominatim_non_us_result_rejected():
    responses.get(
        NOMINATIM_URL,
        json=[_nominatim_result(lat="45.5", lon="-73.6", address={"country_code": "ca"})],
    )

    with pytest.raises(GeocodingError) as excinfo:
        resolve_location("Montreal")
    assert excinfo.value.code == "outside_usa"


@responses.activate
def test_nominatim_us_result_outside_boxes_rejected():
    # Guam is US territory but outside the supported boxes.
    responses.get(NOMINATIM_URL, json=[_nominatim_result(lat="13.44", lon="144.79")])

    with pytest.raises(GeocodingError) as excinfo:
        resolve_location("Hagatna")
    assert excinfo.value.code == "outside_usa"


@responses.activate
def test_nominatim_http_error_is_provider_error():
    responses.get(NOMINATIM_URL, status=503)

    with pytest.raises(GeocodingError) as excinfo:
        resolve_location("Tulsa, OK")
    assert excinfo.value.code == "provider_error"


@responses.activate
def test_external_call_counter_increments_per_nominatim_call():
    responses.get(NOMINATIM_URL, json=[_nominatim_result()])

    resolve_location("Dallas, TX")  # gazetteer, no call
    assert get_external_calls() == 0
    resolve_location("Oklahoma City, OK")
    resolve_location("Oklahoma City, Oklahoma")
    assert get_external_calls() == 2
    assert len(responses.calls) == 2
