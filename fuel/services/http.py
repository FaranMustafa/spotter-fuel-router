"""Shared HTTP plumbing for external services: a configured session and a call counter.

The counter is a ``ContextVar`` so each request (thread or asyncio task) sees its own
value. Views call ``reset_external_calls()`` at the start of a request and report
``get_external_calls()`` in the response ``meta``.
"""

from __future__ import annotations

from contextvars import ContextVar

import requests
from django.conf import settings

external_calls: ContextVar[int] = ContextVar("external_calls", default=0)


def reset_external_calls() -> None:
    """Set the external call counter for the current context back to zero."""
    external_calls.set(0)


def get_external_calls() -> int:
    """Return how many real HTTP calls were made in the current context."""
    return external_calls.get()


def record_external_call() -> None:
    """Increment the counter; call this once per real HTTP request attempt."""
    external_calls.set(external_calls.get() + 1)


def build_session() -> requests.Session:
    """Return a new ``requests.Session`` with the project User-Agent header set."""
    session = requests.Session()
    session.headers["User-Agent"] = settings.NOMINATIM_USER_AGENT
    return session
