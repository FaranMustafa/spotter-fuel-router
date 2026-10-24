"""Geocode every station city/state that has no Place: GeoNames first, Nominatim fallback.

This is the one data-pipeline step that needs the network. Its output is committed as
``fuel/data/geocoded_places.json`` so ``import_fuel_prices`` can run offline.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel.models import FuelStation, Place
from fuel.services import gazetteer

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(settings.BASE_DIR) / "fuel" / "data" / "geocoded_places.json"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0

# The price list also covers Canadian truck stops; they are geocoded against Canada.
CANADIAN_PROVINCES = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}


class NominatimClient:
    """Structured-search client that never exceeds one request per second."""

    def __init__(self, state_names: dict[str, str]) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings.NOMINATIM_USER_AGENT
        self.state_names = state_names
        self.calls = 0
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = NOMINATIM_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def geocode(self, city: str, state: str) -> tuple[float, float] | None:
        """Return ``(lat, lon)`` for the city, or ``None`` on a miss or any error."""
        canadian = state in CANADIAN_PROVINCES
        country, country_code = ("Canada", "CA") if canadian else ("USA", "US")
        state_name = CANADIAN_PROVINCES.get(state) or self.state_names.get(state, state)
        params = {
            "city": city,
            "state": state_name,
            "country": country,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
        }
        self._throttle()
        self.calls += 1
        try:
            response = self.session.get(
                f"{settings.NOMINATIM_BASE_URL}/search",
                params=params,
                timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            results = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Nominatim error for %s, %s: %s", city, state, exc)
            return None
        if not results:
            return None
        hit = results[0]
        iso = (hit.get("address") or {}).get("ISO3166-2-lvl4", "")
        if iso and iso != f"{country_code}-{state}":
            logger.warning("Nominatim result for %s, %s is in %s; ignored", city, state, iso)
            return None
        try:
            return float(hit["lat"]), float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            return None


def export_places(path: Path) -> int:
    """Write every Place to ``path`` as a stable, sorted JSON list."""
    entries = [
        {
            "city": p.city,
            "state": p.state,
            "lat": round(p.lat, 6),
            "lon": round(p.lon, 6),
            "source": p.source,
        }
        for p in Place.objects.order_by("state", "city")
    ]
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(entries)


class Command(BaseCommand):
    help = "Geocode station city/state pairs lacking a Place and export the fixture."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--limit", type=int, default=None, help="Max pairs to geocode.")
        parser.add_argument(
            "--no-nominatim", action="store_true", help="GeoNames only, no network fallback."
        )
        parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH, help="Output JSON.")

    def handle(self, *args: Any, **options: Any) -> None:
        started = time.monotonic()
        index = gazetteer.GeoNamesIndex.from_zip(gazetteer.download_geonames())
        gazetteer.set_geonames_index(index)

        all_pairs = set(FuelStation.objects.values_list("city", "state").distinct())
        known = set(Place.objects.values_list("city", "state"))
        todo = sorted(all_pairs - known, key=lambda pair: (pair[1], pair[0]))
        if options["limit"] is not None:
            todo = todo[: options["limit"]]
        self.stdout.write(f"Pairs needing a Place: {len(todo):,} of {len(all_pairs):,}")

        nominatim = (
            None
            if options["no_nominatim"]
            else NominatimClient({code: name for name, code in index.state_name_to_code.items()})
        )
        new_places: list[Place] = []
        for i, (city, state) in enumerate(todo, start=1):
            coordinates = index.lookup(city, state)
            source = Place.Source.GEONAMES
            if coordinates is None and nominatim is not None:
                coordinates = nominatim.geocode(city, state)
                source = Place.Source.NOMINATIM
                if nominatim.calls % 50 == 0:
                    self.stdout.write(f"  Nominatim calls: {nominatim.calls} ({i}/{len(todo)})")
            if coordinates is not None:
                lat, lon = coordinates
                new_places.append(Place(city=city, state=state, lat=lat, lon=lon, source=source))

        with transaction.atomic():
            Place.objects.bulk_create(new_places)
            for place in Place.objects.all():
                FuelStation.objects.filter(
                    city=place.city, state=place.state, place__isnull=True
                ).update(place=place)
        exported = export_places(options["fixture"])
        gazetteer.reload_station_index()

        self._report(all_pairs, nominatim, exported, time.monotonic() - started)

    def _report(
        self,
        all_pairs: set[tuple[str, str]],
        nominatim: NominatimClient | None,
        exported: int,
        elapsed: float,
    ) -> None:
        by_pair = {
            (c, s): src for c, s, src in Place.objects.values_list("city", "state", "source")
        }
        resolved = {pair: by_pair[pair] for pair in all_pairs if pair in by_pair}
        unresolved = sorted(all_pairs - resolved.keys(), key=lambda pair: (pair[1], pair[0]))
        counts = {src: 0 for src in Place.Source.values}
        for src in resolved.values():
            counts[src] += 1
        total = len(all_pairs) or 1
        self.stdout.write(f"Pairs total: {len(all_pairs):,}")
        for src, n in counts.items():
            self.stdout.write(f"  resolved via {src}: {n:,}")
        self.stdout.write(f"  unresolved: {len(unresolved):,}")
        self.stdout.write(f"Coverage: {100 * len(resolved) / total:.2f}%")
        for city, state in unresolved:
            self.stdout.write(f"    unresolved: {city}, {state}")
        if nominatim is not None:
            self.stdout.write(f"Nominatim calls this run: {nominatim.calls:,}")
        self.stdout.write(f"Places exported: {exported:,}")
        self.stdout.write(self.style.SUCCESS(f"Done in {elapsed:.1f} s"))
