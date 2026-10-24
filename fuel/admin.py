from django.contrib import admin

from fuel.models import FuelStation, Place


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("city", "state", "lat", "lon", "source", "geocoded_at")
    list_filter = ("source", "state")
    search_fields = ("city", "state")


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "retail_price", "place")
    list_filter = ("state",)
    search_fields = ("name", "city", "opis_id")
    raw_id_fields = ("place",)
