"""Shared fixtures for the test suite.

No test here touches the network: every HTTP call is intercepted by respx, and
a missing route makes respx fail loudly rather than reach the real API. That
matters more than usual for this project, since add_delivery is capped at 20
requests per day upstream, failed attempts included.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from parcel_mcp import client

# The mock_carriers fixture hands back a route-registering callable.
MockCarriers = Callable[..., respx.Route]

# Obviously fake, and shaped nothing like a real Parcel key.
FAKE_TOKEN = "token-for-tests-only"
# A second caller, for the HTTP case where one process serves several keys.
OTHER_TOKEN = "another-token-for-tests-only"

DELIVERIES_URL = f"{client.API_BASE}/deliveries/"
ADD_DELIVERY_URL = f"{client.API_BASE}/add-delivery/"

# A trimmed carrier catalogue covering the cases the tools branch on:
# no extra field, extra_required 1 (postcode), 2 (email), and name variations.
CARRIERS: dict[str, dict[str, Any]] = {
    "lp": {"name": "La Poste"},
    "chrono": {
        "name": "Chronopost",
        "name_variations": {"fr": "Chronopost France"},
    },
    "bpost": {"name": "Bpost", "extra_required": 1},
    "applestore": {"name": "Apple Store", "extra_required": 2},
    "pholder": {"name": "Placeholder"},
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fake API key and an empty cache.

    The cache and the rate-limit counters are process-global module state, so
    without this they would leak from one test into the next.
    """
    monkeypatch.setenv("PARCEL_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv("PARCEL_API_KEY", raising=False)
    client.clear_cache()
    client.clear_rate_limits()


class FakeClock:
    """A monotonic clock the tests can move forward by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        """Start the clock at ``start`` seconds."""
        self.now = start

    def monotonic(self) -> float:
        """Return the current fake time, in seconds."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far to jump. Used to step past a cache TTL.
        """
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the ``time`` module inside parcel_mcp.client with a fake clock.

    Only the client module is patched, so nothing else in the process sees a
    frozen clock. ``time.monotonic`` is the module's only use of ``time``.
    """
    fake = FakeClock()
    monkeypatch.setattr(client, "time", fake)
    return fake


@pytest.fixture
def mock_carriers() -> MockCarriers:
    """Return a helper registering the carrier-catalogue route.

    Call it from inside an active respx mock. The catalogue is fetched lazily by
    almost every tool, so nearly every test needs this route.

    Returns:
        A callable taking an optional payload and returning the respx route.
    """

    def register(payload: Any = None, status_code: int = 200) -> respx.Route:
        body = CARRIERS if payload is None else payload
        return respx.get(client.CARRIERS_URL).mock(
            return_value=httpx.Response(status_code, json=body)
        )

    return register


@pytest.fixture
def delivery() -> dict[str, Any]:
    """One delivery in the shape the upstream API returns."""
    return {
        "description": "A parcel",
        "tracking_number": "ABC123",
        "carrier_code": "lp",
        "status_code": 2,
        "date_expected": "2026-09-05",
        "events": [{"event": "In transit", "date": "2026-09-03"}],
    }
