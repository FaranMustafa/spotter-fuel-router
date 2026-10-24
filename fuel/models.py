"""Data model: geocoded places (city/state centroids) and the fuel stations in the price list."""

from django.db import models
from django.utils import timezone


class Place(models.Model):
    """A city/state pair with coordinates, used to locate stations at city level."""

    class Source(models.TextChoices):
        GEONAMES = "geonames", "GeoNames"
        NOMINATIM = "nominatim", "Nominatim"
        MANUAL = "manual", "Manual"

    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    lat = models.FloatField()
    lon = models.FloatField()
    source = models.CharField(max_length=16, choices=Source.choices)
    geocoded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [("city", "state")]
        indexes = [models.Index(fields=["state", "city"], name="fuel_place_state_city_idx")]
        ordering = ["state", "city"]

    def __str__(self) -> str:
        return f"{self.city}, {self.state}"


class FuelStation(models.Model):
    """One deduplicated row of the OPIS price list. `opis_id` repeats across name variants."""

    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField()
    retail_price = models.DecimalField(max_digits=10, decimal_places=5)
    place = models.ForeignKey(
        Place,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stations",
    )

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
