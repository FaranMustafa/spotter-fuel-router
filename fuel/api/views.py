from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views import View
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel.api.errors import describe_exception
from fuel.api.serializers import RouteQuerySerializer, build_route_payload
from fuel.services.trip import plan_trip

# Fields that change the plan, carried into ``map_url`` (``geometry`` only shapes the
# response; the map page always draws the simplified line).
ROUTE_FIELDS = (
    "start",
    "finish",
    "corridor_miles",
    "initial_fuel_gallons",
    "mpg",
    "range_miles",
    "min_stop_savings_usd",
)


class HealthView(APIView):
    """Liveness probe: returns 200 with a static payload."""

    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})


def _map_url(request: HttpRequest, query_string: str) -> str:
    url = reverse("fuel_api:route-map")
    return request.build_absolute_uri(f"{url}?{query_string}" if query_string else url)


def _run(
    data: Any, request: HttpRequest, query_string: str, *, geometry: str | None = None
) -> dict[str, Any]:
    """Validate, plan and serialize; raises ValidationError or the services' typed errors.

    ``geometry`` overrides the requested geometry mode (the map page needs the line)."""
    serializer = RouteQuerySerializer(data=data)
    serializer.is_valid(raise_exception=True)
    params = serializer.validated_data
    result = plan_trip(
        params["start"],
        params["finish"],
        corridor_miles=params["corridor_miles"],
        initial_fuel_gallons=params["initial_fuel_gallons"],
        mpg=params["mpg"],
        range_miles=params["range_miles"],
        min_stop_savings_usd=params["min_stop_savings_usd"],
    )
    mode = geometry or params["geometry"]
    return build_route_payload(result, _map_url(request, query_string), geometry=mode)


class RouteView(APIView):
    """``GET /api/route/?start=&finish=...`` or ``POST`` the same fields as JSON."""

    def get(self, request: Request) -> Response:
        return Response(_run(request.query_params, request, request.query_params.urlencode()))

    def post(self, request: Request) -> Response:
        data = request.data
        # The map page only takes a query string: rebuild one from the fields supplied.
        provided = {k: data[k] for k in ROUTE_FIELDS if hasattr(data, "get") and k in data}
        provided = {k: v for k, v in provided.items() if v is not None}
        return Response(_run(data, request, urlencode(provided)))


class RouteMapView(View):
    """``GET /api/route/map/?start=&finish=...``: the Leaflet page for the same plan."""

    def get(self, request: HttpRequest) -> HttpResponse:
        try:
            payload = _run(request.GET, request, request.GET.urlencode(), geometry="simplified")
        except Exception as exc:  # every failure is rendered on the page, like the API
            status_code, body = describe_exception(exc)
            context = {"payload": None, "error": body["error"], "query": request.GET}
            return render(request, "fuel/map.html", context, status=status_code)
        return render(request, "fuel/map.html", {"payload": payload, "error": None})
