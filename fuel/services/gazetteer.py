"""Local gazetteer: city/state lookup and the in-memory station arrays.

Two in-process caches live here, each built lazily and kept for the life of the process:

- the GeoNames US postal-code index (``(normalized city, state code) -> centroid``), read
  from ``.cache/geonames/US.zip``. Only the management commands download that file; at
  runtime a missing file simply means the index is empty and lookups fall through to the
  ``Place`` table alone.
- the ``StationIndex``: numpy arrays of every station that has coordinates, consumed by
  the corridor computation. ``reload_station_index()`` drops it after an import.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from django.conf import settings

from fuel.models import FuelStation, Place

logger = logging.getLogger(__name__)

GEONAMES_URL = "https://download.geonames.org/export/zip/US.zip"
GEONAMES_MEMBER = "US.txt"


def geonames_zip_path() -> Path:
    """Where the GeoNames US postal file is cached (gitignored)."""
    return Path(settings.BASE_DIR) / ".cache" / "geonames" / "US.zip"


# --------------------------------------------------------------------------- normalization

_ABBREVIATIONS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}
# Hyphens and slashes separate words ("Winston-Salem"); every other punctuation mark is
# dropped ("O'Fallon" -> "ofallon", "St." -> "st").
_SEPARATORS = re.compile(r"[-/]")
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_city(name: str) -> str:
    """Return a comparison key for a city name.

    Case-folds, removes punctuation, collapses whitespace and expands a leading
    "St"/"Ste"/"Ft"/"Mt" abbreviation: ``"Ft. Worth" -> "fort worth"``,
    ``"St Louis" -> "saint louis"``.
    """
    text = _SEPARATORS.sub(" ", name.casefold())
    text = _PUNCTUATION.sub("", text).replace("_", " ")
    words = _WHITESPACE.sub(" ", text).strip().split(" ")
    if words and words[0] in _ABBREVIATIONS and len(words) > 1:
        words[0] = _ABBREVIATIONS[words[0]]
    return " ".join(w for w in words if w)


# --------------------------------------------------------------------------- GeoNames index

# Columns of the GeoNames postal-code dump (tab-separated, no header).
_COL_PLACE = 2
_COL_STATE_NAME = 3
_COL_STATE_CODE = 4
_COL_LAT = 9
_COL_LON = 10


class GeoNamesIndex:
    """``(normalize_city(place_name), state_code) -> (lat, lon)`` from GeoNames postal rows.

    Each key maps to the mean coordinate of all postal codes sharing that place name in
    that state (a cheap centroid). ``state_name_to_code`` maps "Texas" -> "TX".
    """

    def __init__(
        self,
        coordinates: dict[tuple[str, str], tuple[float, float]],
        state_name_to_code: dict[str, str],
        postal_code_count: int = 0,
    ) -> None:
        self.coordinates = coordinates
        self.state_name_to_code = state_name_to_code
        self.postal_code_count = postal_code_count
        # Space-insensitive fallback ("Mc Leansville" ~ "McLeansville", "De Forest" ~
        # "Deforest"); a compact key shared by two different places is ambiguous and dropped.
        compact: dict[tuple[str, str], tuple[float, float] | None] = {}
        for (city, state), point in coordinates.items():
            key = (city.replace(" ", ""), state)
            compact[key] = None if key in compact else point
        self._compact = {key: point for key, point in compact.items() if point is not None}

    def __len__(self) -> int:
        return len(self.coordinates)

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence[str]]) -> GeoNamesIndex:
        """Build from already-split GeoNames rows (12 columns each)."""
        sums: dict[tuple[str, str], list[float]] = {}
        state_names: dict[str, str] = {}
        postal_codes = 0
        for row in rows:
            if len(row) <= _COL_LON:
                continue
            state_code = row[_COL_STATE_CODE].strip().upper()
            state_name = row[_COL_STATE_NAME].strip()
            try:
                lat = float(row[_COL_LAT])
                lon = float(row[_COL_LON])
            except ValueError:
                continue
            key_city = normalize_city(row[_COL_PLACE])
            if not key_city or not state_code:
                continue
            postal_codes += 1
            if state_name:
                state_names.setdefault(state_name, state_code)
            acc = sums.setdefault((key_city, state_code), [0.0, 0.0, 0.0])
            acc[0] += lat
            acc[1] += lon
            acc[2] += 1
        coordinates = {key: (acc[0] / acc[2], acc[1] / acc[2]) for key, acc in sums.items()}
        return cls(coordinates, state_names, postal_codes)

    @classmethod
    def from_text(cls, stream: Iterable[str]) -> GeoNamesIndex:
        """Build from a text stream of the tab-separated ``US.txt`` file."""
        reader = csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        return cls.from_rows(reader)

    @classmethod
    def from_zip(cls, path: Path) -> GeoNamesIndex:
        """Build from the downloaded ``US.zip`` (reads its ``US.txt`` member)."""
        with zipfile.ZipFile(path) as archive, archive.open(GEONAMES_MEMBER) as raw:
            return cls.from_text(io.TextIOWrapper(raw, encoding="utf-8", newline=""))

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        """Return the centroid for a city in a state code, or ``None``."""
        key = (normalize_city(city), state.strip().upper())
        point = self.coordinates.get(key)
        if point is None:
            point = self._compact.get((key[0].replace(" ", ""), key[1]))
        return point


def download_geonames(path: Path | None = None, *, force: bool = False) -> Path:
    """Download ``US.zip`` once into the cache dir. Used only by management commands."""
    import requests  # local import: only the offline-data commands download anything

    target = path or geonames_zip_path()
    if target.exists() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s", GEONAMES_URL)
    response = requests.get(
        GEONAMES_URL,
        headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
        timeout=settings.HTTP_TIMEOUT_SECONDS,
        stream=True,
    )
    response.raise_for_status()
    partial = target.with_suffix(".zip.part")
    with partial.open("wb") as fh:
        for chunk in response.iter_content(chunk_size=1 << 16):
            fh.write(chunk)
    partial.replace(target)
    return target


_geonames_index: GeoNamesIndex | None = None


def get_geonames_index() -> GeoNamesIndex:
    """Return the process-wide GeoNames index, building it from the cached zip on first use.

    Never downloads: if the zip is missing the index is empty (lookups then rely on the
    ``Place`` table, which covers every city in the price list).
    """
    global _geonames_index
    if _geonames_index is None:
        path = geonames_zip_path()
        if path.exists():
            _geonames_index = GeoNamesIndex.from_zip(path)
        else:
            logger.warning("GeoNames file %s missing; run build_gazetteer", path)
            _geonames_index = GeoNamesIndex({}, {})
    return _geonames_index


def set_geonames_index(index: GeoNamesIndex | None) -> None:
    """Replace (or with ``None``, clear) the process-wide GeoNames index."""
    global _geonames_index
    _geonames_index = index


# --------------------------------------------------------------------------- place lookup


def lookup_place(city: str, state: str) -> Place | None:
    """Resolve a city and two-letter state code to a ``Place``.

    Checks the ``Place`` table first (exact case-insensitive name, then normalized name
    within the state), then the GeoNames index, returning an unsaved
    ``Place(source="geonames")`` for a GeoNames hit. Returns ``None`` when neither knows it.
    """
    city = city.strip()
    state = state.strip().upper()
    if not city or len(state) != 2:
        return None
    place = Place.objects.filter(city__iexact=city, state=state).first()
    if place is not None:
        return place
    key = normalize_city(city)
    for candidate in Place.objects.filter(state=state):
        if normalize_city(candidate.city) == key:
            return candidate
    coordinates = get_geonames_index().lookup(city, state)
    if coordinates is None:
        return None
    lat, lon = coordinates
    return Place(city=city, state=state, lat=lat, lon=lon, source=Place.Source.GEONAMES)


# --------------------------------------------------------------------------- station index


@dataclass(frozen=True)
class StationIndex:
    """Parallel arrays over every station whose place has coordinates."""

    ids: np.ndarray  # int64, FuelStation primary keys
    lats: np.ndarray  # float64, degrees
    lons: np.ndarray  # float64, degrees
    prices: np.ndarray  # float64, USD per gallon
    station_count: int


_station_index: StationIndex | None = None


US_STATE_CODES: frozenset[str] = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    ]
)


def _load_station_index() -> StationIndex:
    # The assignment is US-only: Canadian truck stops in the CSV stay in the DB but are
    # never offered as fuel stops.
    rows = list(
        FuelStation.objects.filter(place__isnull=False, place__state__in=US_STATE_CODES)
        .order_by("id")
        .values_list("id", "place__lat", "place__lon", "retail_price")
    )
    if rows:
        ids, lats, lons, prices = zip(*rows, strict=True)
    else:
        ids = lats = lons = prices = ()
    return StationIndex(
        ids=np.asarray(ids, dtype=np.int64),
        lats=np.asarray(lats, dtype=np.float64),
        lons=np.asarray(lons, dtype=np.float64),
        prices=np.asarray([float(p) for p in prices], dtype=np.float64),
        station_count=len(rows),
    )


def get_station_index() -> StationIndex:
    """Return the process-wide station arrays, loading them from the DB on first use."""
    global _station_index
    if _station_index is None:
        _station_index = _load_station_index()
    return _station_index


def reload_station_index() -> None:
    """Drop the cached station arrays; the next ``get_station_index()`` reloads them."""
    global _station_index
    _station_index = None
