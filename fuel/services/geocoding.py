"""Resolve free-text start/finish inputs to US coordinates.

Order: explicit ``lat,lon`` -> local gazetteer -> Nominatim. Every result is checked to
lie inside the continental US, Alaska or Hawaii bounding boxes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from django.conf import settings

from fuel.services.http import build_session, record_external_call

STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
STATE_CODES: frozenset[str] = frozenset(STATE_NAMES.values())
# Canadian province/territory codes: "Toronto, ON" is rejected before any network call.
CANADIAN_PROVINCE_CODES: frozenset[str] = frozenset(
    ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"]
)

# (min_lat, max_lat, min_lon, max_lon)
USA_BOXES: tuple[tuple[float, float, float, float], ...] = (
    (24.5, 49.5, -125.0, -66.9),  # continental
    (51.0, 72.0, -180.0, -129.0),  # Alaska
    (18.5, 22.5, -161.0, -154.0),  # Hawaii
)

_COORD_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    label: str
    source: str  # "coordinates" | "gazetteer" | "nominatim"


class GeocodingError(Exception):
    """``code`` is one of invalid_input, not_found, outside_usa, provider_error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class _Place(Protocol):
    lat: float
    lon: float
    city: str
    state: str


def assert_inside_usa(lat: float, lon: float) -> None:
    """Raise ``GeocodingError("outside_usa")`` unless the point is in a US bounding box."""
    for min_lat, max_lat, min_lon, max_lon in USA_BOXES:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return
    raise GeocodingError(
        "outside_usa", f"Location ({lat:.4f}, {lon:.4f}) is outside the United States."
    )


def _parse_coordinates(text: str) -> tuple[float, float] | None:
    match = _COORD_RE.match(text)
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not -90.0 <= lat <= 90.0:
        raise GeocodingError("invalid_input", f"Latitude {lat} is out of range (-90 to 90).")
    if not -180.0 <= lon <= 180.0:
        raise GeocodingError("invalid_input", f"Longitude {lon} is out of range (-180 to 180).")
    return lat, lon


def _state_code(text: str) -> str | None:
    cleaned = " ".join(text.replace(".", " ").split()).lower()
    if cleaned.upper() in STATE_CODES:
        return cleaned.upper()
    return STATE_NAMES.get(cleaned)


def _parse_city_state(text: str) -> tuple[str, str] | None:
    """Split "City, ST", "City ST", "City, State Name" or "City State Name".

    Raises ``GeocodingError("outside_usa")`` if the text ends with a Canadian province code.
    """
    last = text.replace(",", " ").split()[-1:]
    if last and len(text.split()) > 1 and last[0].upper() in CANADIAN_PROVINCE_CODES:
        raise GeocodingError("outside_usa", f"'{text}' is in Canada, not the USA.")
    if "," in text:
        city, _, state_part = text.rpartition(",")
        code = _state_code(state_part)
        city = " ".join(city.split())
        return (city, code) if city and code else None

    words = text.split()
    # Longest state-name suffix first ("West Virginia" before "Virginia").
    for size in (3, 2, 1):
        if len(words) <= size:
            continue
        code = _state_code(" ".join(words[-size:]))
        if code:
            return " ".join(words[:-size]), code
    return None


def _gazetteer_lookup(city: str, state: str) -> _Place | None:
    """Local gazetteer lookup; the single patch point for tests."""
    try:
        from fuel.services.gazetteer import lookup_place
    except ImportError:
        return None
    return lookup_place(city, state)


def _nominatim_label(result: dict[str, Any]) -> str:
    address = result.get("address") or {}
    city = next(
        (address[k] for k in ("city", "town", "village", "hamlet", "municipality") if k in address),
        None,
    )
    iso = str(address.get("ISO3166-2-lvl4", ""))
    state = iso[3:] if iso.startswith("US-") else _state_code(str(address.get("state", "")))
    if city and state:
        return f"{city}, {state}"
    parts = [p.strip() for p in str(result.get("display_name", "")).split(",") if p.strip()]
    if parts:
        return ", ".join(parts[:2])
    return str(result.get("name") or "")


def _nominatim_lookup(query: str) -> Location:
    url = f"{settings.NOMINATIM_BASE_URL.rstrip('/')}/search"
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": "1",
        "addressdetails": "1",
    }
    record_external_call()
    try:
        response = build_session().get(
            url,
            params=params,
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
            timeout=settings.HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise GeocodingError("provider_error", f"Geocoding service unavailable: {exc}") from exc
    if response.status_code != 200:
        raise GeocodingError(
            "provider_error", f"Geocoding service returned HTTP {response.status_code}."
        )
    try:
        results = response.json()
    except ValueError as exc:
        raise GeocodingError("provider_error", "Geocoding service returned invalid JSON.") from exc
    if not isinstance(results, list):
        raise GeocodingError("provider_error", "Geocoding service returned an unexpected payload.")
    if not results:
        raise GeocodingError("not_found", f"Could not find a location for '{query}'.")

    result = results[0]
    country = str((result.get("address") or {}).get("country_code", "")).lower()
    if country != "us":
        # No countrycodes filter on the request: a foreign best match must be rejected, not
        # silently replaced by the closest-sounding US place.
        address = result.get("address") or {}
        where = str(address.get("country") or country.upper() or "a location outside the USA")
        raise GeocodingError("outside_usa", f"'{query}' resolves to {where}, not the USA.")
    try:
        lat, lon = float(result["lat"]), float(result["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodingError(
            "provider_error", "Geocoding service returned a result without coordinates."
        ) from exc
    return Location(lat=lat, lon=lon, label=_nominatim_label(result) or query, source="nominatim")


def resolve_location(text: str) -> Location:
    """Resolve "lat,lon", "City, ST", "City ST" or "City, State Name" to a US Location."""
    if not isinstance(text, str) or not text.strip():
        raise GeocodingError("invalid_input", "Location must be a non-empty string.")
    cleaned = " ".join(text.split())

    coords = _parse_coordinates(cleaned)
    if coords is not None:
        lat, lon = coords
        assert_inside_usa(lat, lon)
        return Location(lat=lat, lon=lon, label=f"{lat:.4f}, {lon:.4f}", source="coordinates")

    parsed = _parse_city_state(cleaned)
    if parsed is not None:
        city, state = parsed
        place = _gazetteer_lookup(city, state)
        if place is not None:
            lat, lon = float(place.lat), float(place.lon)
            assert_inside_usa(lat, lon)
            return Location(
                lat=lat, lon=lon, label=f"{place.city}, {place.state}", source="gazetteer"
            )
        query = f"{city}, {state}"
    else:
        query = cleaned

    location = _nominatim_lookup(query)
    assert_inside_usa(location.lat, location.lon)
    return location
