"""Download (once) and index the GeoNames US postal-code file."""

from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand

from fuel.services import gazetteer


class Command(BaseCommand):
    help = "Download the GeoNames US postal file (if missing) and build the place index."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--force-download", action="store_true", help="Re-download even if cached."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        started = time.monotonic()
        path = gazetteer.download_geonames(force=options["force_download"])
        index = gazetteer.GeoNamesIndex.from_zip(path)
        gazetteer.set_geonames_index(index)
        elapsed = time.monotonic() - started
        self.stdout.write(f"GeoNames file: {path} ({path.stat().st_size:,} bytes)")
        self.stdout.write(f"Postal codes read: {index.postal_code_count:,}")
        self.stdout.write(f"Place keys (city, state): {len(index):,}")
        self.stdout.write(f"States/territories: {len(index.state_name_to_code)}")
        self.stdout.write(self.style.SUCCESS(f"Gazetteer built in {elapsed:.1f} s"))
