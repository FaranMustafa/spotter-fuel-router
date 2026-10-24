"""Load the OPIS price list into FuelStation rows, linked to geocoded Places. No network."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel.models import FuelStation, Place
from fuel.services.gazetteer import reload_station_index

DATA_DIR = Path(settings.BASE_DIR) / "fuel" / "data"
DEFAULT_CSV = DATA_DIR / "fuel-prices.csv"
DEFAULT_FIXTURE = DATA_DIR / "geocoded_places.json"
PRICE_QUANTUM = Decimal("0.00001")


@dataclass(frozen=True)
class StationRow:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int
    retail_price: Decimal

    @property
    def dedupe_key(self) -> tuple[int, str, str, Decimal]:
        return (self.opis_id, self.city, self.state, self.retail_price)


def read_station_rows(path: Path) -> list[StationRow]:
    """Parse the price CSV (CRLF tolerant, whitespace stripped, state upper-cased)."""
    rows: list[StationRow] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for line_no, raw in enumerate(reader, start=2):
            record = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
            try:
                rows.append(
                    StationRow(
                        opis_id=int(record["OPIS Truckstop ID"]),
                        name=record["Truckstop Name"],
                        address=record["Address"],
                        city=record["City"],
                        state=record["State"].upper(),
                        rack_id=int(record["Rack ID"]),
                        retail_price=Decimal(record["Retail Price"]).quantize(PRICE_QUANTUM),
                    )
                )
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise CommandError(f"{path}:{line_no}: cannot parse row: {exc}") from exc
    return rows


def dedupe_rows(rows: list[StationRow]) -> list[StationRow]:
    """Keep the first row per (opis_id, city, state, retail_price); later name variants drop."""
    seen: set[tuple[int, str, str, Decimal]] = set()
    unique: list[StationRow] = []
    for row in rows:
        if row.dedupe_key in seen:
            continue
        seen.add(row.dedupe_key)
        unique.append(row)
    return unique


def load_places_fixture(path: Path) -> int:
    """Upsert every ``{city, state, lat, lon, source}`` entry into ``Place``."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        Place.objects.update_or_create(
            city=entry["city"],
            state=entry["state"],
            defaults={"lat": entry["lat"], "lon": entry["lon"], "source": entry["source"]},
        )
    return len(entries)


class Command(BaseCommand):
    help = "Import fuel-prices.csv into FuelStation (idempotent) and load the geocode fixture."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Price list CSV.")
        parser.add_argument(
            "--fixture", type=Path, default=DEFAULT_FIXTURE, help="Geocoded places JSON."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        csv_path: Path = options["csv"]
        fixture_path: Path = options["fixture"]
        if not csv_path.exists():
            raise CommandError(f"CSV not found: {csv_path}")

        rows = read_station_rows(csv_path)
        unique = dedupe_rows(rows)

        with transaction.atomic():
            if fixture_path.exists():
                loaded = load_places_fixture(fixture_path)
                self.stdout.write(f"Places loaded from fixture: {loaded:,}")
            else:
                self.stdout.write(self.style.WARNING(f"No fixture at {fixture_path}"))
            places = {(p.city, p.state): p for p in Place.objects.all()}
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(
                [
                    FuelStation(
                        opis_id=row.opis_id,
                        name=row.name,
                        address=row.address,
                        city=row.city,
                        state=row.state,
                        rack_id=row.rack_id,
                        retail_price=row.retail_price,
                        place=places.get((row.city, row.state)),
                    )
                    for row in unique
                ],
                batch_size=1000,
            )
        missing = sum(1 for row in unique if (row.city, row.state) not in places)
        reload_station_index()

        self.stdout.write(f"Rows read: {len(rows):,}")
        self.stdout.write(f"Duplicates dropped: {len(rows) - len(unique):,}")
        self.stdout.write(f"Stations created: {len(unique):,}")
        self.stdout.write(f"Stations without coordinates: {missing:,}")
        self.stdout.write(self.style.SUCCESS("Import complete"))
