"""HTTP access to the Parcel (parcelapp.net) external API.

Everything that talks to the network lives here: API-key handling, the
``success``/``error_message`` envelope, the carrier catalogue, the TTL cache
and the local rate-limit counters.

Two constraints shape this module.

Parcel sends no rate-limit headers of any kind, only ``Date``. A caller that
wants to pace itself has nothing to read, so the budget reported by
:func:`budget` is counted here and is an estimate, never an authority.

The server can also run over HTTP, where each request carries its own API key.
Anything cached or counted is therefore partitioned by a fingerprint of the
key: one caller's deliveries must never be served to another. The carrier
catalogue is the exception, since it is public and unauthenticated.

This module knows nothing about MCP. The one exception is ``ParcelError``,
which subclasses the SDK's ``ToolError`` so that failure messages survive the
trip to the client, and that is a deliberate trade.
"""

from __future__ import annotations

import hashlib
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

DELIVERIES_BUCKET = "deliveries"
ADD_BUCKET = "add_delivery"

# bucket -> (requests allowed, window in seconds, window as a word).
# Transcribed from the two help pages; the API itself never states them.
BUCKETS: dict[str, tuple[int, float, str]] = {
    DELIVERIES_BUCKET: (20, 3600.0, "hour"),
    ADD_BUCKET: (20, 86_400.0, "day"),
}

# key -> (monotonic timestamp of the write, value)
_cache: dict[str, tuple[float, Any]] = {}

# "bucket:fingerprint" -> monotonic timestamps of the requests this process sent
_requests: dict[str, list[float]] = {}


class ParcelError(ToolError):
    """Upstream returned success=false, or the transport failed.

    Subclasses the SDK's ToolError so the message reaches the client instead of
    being replaced by a generic "error executing tool".
    """


def api_key() -> str:
    """Read the Parcel API key from the environment.

    This is the stdio path. Over HTTP the key arrives per request instead, and
    the environment is only the fallback.

    Returns:
        The key from ``PARCEL_TOKEN``, falling back to ``PARCEL_API_KEY``.

    Raises:
        ParcelError: Neither variable is set.
    """
    key = os.environ.get("PARCEL_TOKEN") or os.environ.get("PARCEL_API_KEY")
    if not key:
        raise ParcelError(
            "No API key. Set PARCEL_TOKEN (e.g. via `envchain parcel ...`), or, when "
            "running over HTTP, send it as an `X-Parcel-Token` or `Authorization: "
            "Bearer` header. Generate one at https://web.parcelapp.net."
        )
    return key.strip()


def key_fingerprint(key: str) -> str:
    """Derive a short, stable, non-reversible identifier for an API key.

    Used to partition the cache and the request counters between callers when
    several share one HTTP server. The key itself is never used as a dictionary
    key and never logged, so a cache dump cannot leak a credential.

    Args:
        key: The caller's API key.

    Returns:
        The first 16 hex characters of the key's SHA-256 digest.
    """
    return hashlib.sha256(key.encode()).hexdigest()[:16]


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


def cache_age(key: str) -> float | None:
    """Return how many seconds ago ``key`` was written, or None if absent.

    Reported to callers so they can tell a fresh fetch from a cached one
    instead of guessing.

    Args:
        key: Cache key.

    Returns:
        Age in seconds, or None when the key was never written.
    """
    entry = _cache.get(key)
    return None if entry is None else time.monotonic() - entry[0]


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


def clear_rate_limits() -> None:
    """Forget every recorded request. For tests; the counters are per-process."""
    _requests.clear()


def record_request(bucket: str, fingerprint: str) -> None:
    """Note that one request against ``bucket`` was just spent.

    Args:
        bucket: One of the keys of :data:`BUCKETS`.
        fingerprint: The caller's key fingerprint, so budgets do not bleed
            between the callers of a shared HTTP server.
    """
    _requests.setdefault(f"{bucket}:{fingerprint}", []).append(time.monotonic())


def budget(bucket: str, fingerprint: str) -> dict[str, Any]:
    """Report how much of a rate-limit budget this process has spent.

    Honest about what it cannot know: Parcel publishes no counter, so this
    counts only the requests this server sent, in this process, since it
    started. The Parcel app itself and any other client using the same key
    spend from the same budget invisibly, which is why the figure is called
    ``remaining_at_most`` rather than ``remaining``.

    Args:
        bucket: One of the keys of :data:`BUCKETS`.
        fingerprint: The caller's key fingerprint.

    Returns:
        A dict with the documented limit and its window, the number of requests
        this server sent inside that window, an upper bound on what is left,
        and a note carrying the caveat to whoever reads the tool output.
    """
    limit, window, unit = BUCKETS[bucket]
    entry_key = f"{bucket}:{fingerprint}"
    cutoff = time.monotonic() - window
    # Prune while reading: the windows are short and the volumes tiny.
    recent = [t for t in _requests.get(entry_key, []) if t > cutoff]
    _requests[entry_key] = recent
    return {
        "limit": limit,
        "per": unit,
        "spent_by_this_server": len(recent),
        "remaining_at_most": max(limit - len(recent), 0),
        "note": (
            "Counted locally: Parcel returns no rate-limit headers. This server sees "
            "only its own requests since it started, so the real remaining budget may "
            "be lower if the Parcel app or another client shares this key."
        ),
    }


def request(method: str, url: str, key: str, **kwargs: Any) -> dict[str, Any]:
    """Call the Parcel API and unwrap its ``success``/``error_message`` envelope.

    The single HTTP chokepoint: every upstream failure mode is normalised into a
    ParcelError here, so callers never inspect status codes themselves.

    Args:
        method: HTTP method.
        url: Absolute URL.
        key: The caller's API key, sent in the ``api-key`` header.
        **kwargs: Passed through to httpx (``params``, ``json``, ...).

    Returns:
        The decoded JSON body, guaranteed to be a dict with ``success`` true.

    Raises:
        ParcelError: Transport failure, HTTP 401 or 429, a non-JSON body, an
            unexpected JSON shape, or ``success: false``.
    """
    headers = {"api-key": key, "accept": "application/json"}
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

    The list is unauthenticated and not rate-limited, so unlike the delivery
    data its cache is shared across callers.

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
