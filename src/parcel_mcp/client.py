"""HTTP access to the Parcel (parcelapp.net) external API.

Everything that talks to the network lives here: the API-key lookup, the
``success``/``error_message`` envelope handling, the carrier catalogue, and the
small TTL cache that keeps the tools inside the upstream rate limits.

This module knows nothing about MCP. The one exception is ``ParcelError``,
which subclasses the SDK's ``ToolError`` so that failure messages survive the
trip to the client, and that is a deliberate trade.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

API_BASE = "https://api.parcel.app/external"
CARRIERS_URL = f"{API_BASE}/supported_carriers.json"

# Upstream allows 20 deliveries calls per hour and always serves a cached
# response anyway, so a short local cache costs nothing and buys headroom.
DELIVERIES_TTL = 180.0
CARRIERS_TTL = 86_400.0
TIMEOUT = httpx.Timeout(20.0)

# key -> (monotonic timestamp of the write, value)
_cache: dict[str, tuple[float, Any]] = {}


class ParcelError(ToolError):
    """Upstream returned success=false, or the transport failed.

    Subclasses the SDK's ToolError so the message reaches the client instead of
    being replaced by a generic "error executing tool".
    """


def api_key() -> str:
    """Read the Parcel API key from the environment.

    Returns:
        The key from ``PARCEL_TOKEN``, falling back to ``PARCEL_API_KEY``.

    Raises:
        ParcelError: Neither variable is set.
    """
    key = os.environ.get("PARCEL_TOKEN") or os.environ.get("PARCEL_API_KEY")
    if not key:
        raise ParcelError(
            "No API key. Set PARCEL_TOKEN (e.g. via `envchain parcel ...`); "
            "generate one at https://web.parcelapp.net."
        )
    return key.strip()


def cached(key: str, ttl: float) -> Any | None:
    """Return the cached value for ``key``, or None if absent or older than ``ttl``.

    Args:
        key: Cache key.
        ttl: Maximum age in seconds.

    Returns:
        The cached value, or None. A cached None is indistinguishable from a
        miss, which is fine: no caller stores one.
    """
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    return None


def store(key: str, value: Any) -> Any:
    """Cache ``value`` under ``key``, stamped with the current time.

    Args:
        key: Cache key.
        value: Value to cache.

    Returns:
        ``value``, so callers can store and return in one expression.
    """
    _cache[key] = (time.monotonic(), value)
    return value


def clear_cache(*keys: str) -> None:
    """Drop cache entries.

    Args:
        keys: Entries to drop. Passing no key clears the whole cache, which is
            what tests want between cases.
    """
    if not keys:
        _cache.clear()
        return
    for key in keys:
        _cache.pop(key, None)


def request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """Call the Parcel API and unwrap its ``success``/``error_message`` envelope.

    The single HTTP chokepoint: every upstream failure mode is normalised into a
    ParcelError here, so callers never inspect status codes themselves.

    Args:
        method: HTTP method.
        url: Absolute URL.
        **kwargs: Passed through to httpx (``params``, ``json``, ...).

    Returns:
        The decoded JSON body, guaranteed to be a dict with ``success`` true.

    Raises:
        ParcelError: Transport failure, HTTP 401 or 429, a non-JSON body, an
            unexpected JSON shape, or ``success: false``.
    """
    headers = {"api-key": api_key(), "accept": "application/json"}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:  # network, DNS, timeout...
        raise ParcelError(f"Request to Parcel failed: {exc}") from exc

    if response.status_code == 401:
        raise ParcelError("Parcel rejected the API key (HTTP 401).")
    if response.status_code == 429:
        raise ParcelError(
            "Parcel rate limit hit (HTTP 429): 20 delivery listings per hour, 20 additions per day."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ParcelError(
            f"Parcel returned non-JSON content (HTTP {response.status_code})."
        ) from exc

    if not isinstance(payload, dict):
        raise ParcelError("Parcel returned an unexpected JSON shape.")
    if not payload.get("success", False):
        raise ParcelError(
            payload.get("error_message") or f"Parcel request failed (HTTP {response.status_code})."
        )
    return payload


def load_carriers() -> dict[str, dict[str, Any]]:
    """Fetch the public carrier catalogue, cached for 24 hours.

    The list is unauthenticated and not rate-limited, but it is large and
    changes rarely, hence the long TTL.

    Returns:
        Carrier code -> carrier record, as served by Parcel.

    Raises:
        ParcelError: The list could not be fetched or decoded.
    """
    hit: dict[str, dict[str, Any]] | None = cached("carriers", CARRIERS_TTL)
    if hit is not None:
        return hit
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(CARRIERS_URL)
            response.raise_for_status()
            carriers: dict[str, dict[str, Any]] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ParcelError(f"Could not fetch the carrier list: {exc}") from exc
    store("carriers", carriers)
    return carriers


def carrier_name(code: str) -> str:
    """Resolve a carrier code to its display name, degrading to the code itself.

    Deliberately swallows ParcelError: a carrier-list outage should downgrade a
    delivery listing to raw codes, not break it.

    Args:
        code: Internal carrier code, e.g. "lp".

    Returns:
        The carrier's name, or ``code`` when it is unknown or unreachable.
    """
    try:
        name = load_carriers().get(code, {}).get("name", code)
    except ParcelError:
        return code
    # The catalogue is undocumented: a missing or null name falls back to the
    # code rather than rendering as "None".
    return name if isinstance(name, str) else code
