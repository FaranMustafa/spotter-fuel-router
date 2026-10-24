"""Map service errors to the uniform ``{"error": {code, message, details}}`` shape.

``describe_exception`` is shared by the DRF exception handler (JSON API) and the map page
(HTML), so both report the same status code and message for the same failure.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import exceptions, status
from rest_framework.response import Response

from fuel.services.geocoding import GeocodingError
from fuel.services.planner import UnreachableError
from fuel.services.routing import RoutingError

logger = logging.getLogger(__name__)

GEOCODING_CLIENT_CODES = {"invalid_input", "not_found", "outside_usa"}
ROUTING_UNAVAILABLE_CODES = {"timeout", "http_error"}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _validation_details(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        return {
            str(field): [str(m) for m in (msgs if isinstance(msgs, list) else [msgs])]
            for field, msgs in detail.items()
        }
    if isinstance(detail, list):
        return {"non_field_errors": [str(m) for m in detail]}
    return {"non_field_errors": [str(detail)]}


def describe_exception(exc: Exception) -> tuple[int, dict[str, Any]]:
    """Return ``(http_status, error_body)`` for any exception (unknown ones become 500)."""
    if isinstance(exc, exceptions.ValidationError):
        details = _validation_details(exc.detail)
        first = next(iter(details.items()), None)
        message = f"Invalid request: {first[0]}: {first[1][0]}" if first else "Invalid request."
        return status.HTTP_400_BAD_REQUEST, error_body("invalid_request", message, details)

    if isinstance(exc, GeocodingError):
        if exc.code in GEOCODING_CLIENT_CODES:
            return status.HTTP_400_BAD_REQUEST, error_body(exc.code, exc.message)
        return status.HTTP_502_BAD_GATEWAY, error_body("geocoding_unavailable", exc.message)

    if isinstance(exc, RoutingError):
        if exc.code == "NoRoute":
            return status.HTTP_422_UNPROCESSABLE_ENTITY, error_body("no_route", exc.message)
        if exc.code in ROUTING_UNAVAILABLE_CODES:
            return status.HTTP_502_BAD_GATEWAY, error_body("routing_unavailable", exc.message)
        if exc.code == "invalid_response":
            return status.HTTP_502_BAD_GATEWAY, error_body("routing_invalid_response", exc.message)
        # Any other OSRM code (InvalidQuery, NoSegment, TooBig, ...): the provider rejected
        # the request for these points; treat it like "no route".
        return status.HTTP_422_UNPROCESSABLE_ENTITY, error_body(
            "no_route", exc.message, {"provider_code": exc.code}
        )

    if isinstance(exc, UnreachableError):
        details = {
            "from_miles": round(exc.from_miles, 1),
            "to_miles": round(exc.to_miles, 1),
            "range_miles": round(exc.range_miles, 1),
            "suggestion": "Increase corridor_miles or range_miles",
        }
        return status.HTTP_422_UNPROCESSABLE_ENTITY, error_body("unreachable", str(exc), details)

    if isinstance(exc, exceptions.APIException):
        # Framework errors (malformed JSON, method not allowed, ...).
        return exc.status_code, error_body(str(exc.default_code), str(exc.detail))

    logger.exception("Unhandled error while planning a route", exc_info=exc)
    return status.HTTP_500_INTERNAL_SERVER_ERROR, error_body(
        "internal_error", "An unexpected error occurred."
    )


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    """DRF ``EXCEPTION_HANDLER``: every exception becomes the uniform error shape."""
    status_code, body = describe_exception(exc)
    response = Response(body, status=status_code)
    if isinstance(exc, exceptions.APIException):
        auth_header = getattr(exc, "auth_header", None)
        if auth_header:
            response["WWW-Authenticate"] = auth_header
        wait = getattr(exc, "wait", None)
        if wait is not None:
            response["Retry-After"] = str(int(wait))
    return response
