"""MCP server exposing the Parcel (parcelapp.net) external API.

Two upstream endpoints are wrapped:

* ``GET  /external/deliveries/``   - recent or active deliveries (20 req/hour)
* ``POST /external/add-delivery/`` - add one delivery          (20 req/day)

A third tool searches the public carrier-code list, since both endpoints speak
in internal carrier codes rather than human names.

Authentication: an API key generated at https://web.parcelapp.net, read from
``PARCEL_TOKEN`` (or ``PARCEL_API_KEY``) and sent in the ``api-key`` header.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

LOG = logging.getLogger("parcel-mcp")

API_BASE = "https://api.parcel.app/external"
CARRIERS_URL = f"{API_BASE}/supported_carriers.json"

# Upstream allows 20 deliveries calls per hour and always serves a cached
# response anyway, so a short local cache costs nothing and buys headroom.
DELIVERIES_TTL = 180.0
CARRIERS_TTL = 86_400.0
TIMEOUT = httpx.Timeout(20.0)

STATUS_CODES: dict[int, str] = {
    0: "completed",
    1: "frozen (no updates for a long time)",
    2: "in transit",
    3: "awaiting pickup by recipient",
    4: "out for delivery",
    5: "not found",
    6: "failed delivery attempt",
    7: "exception - requires attention",
    8: "carrier notified, package not yet received",
}

# "extra_required" values seen in supported_carriers.json.
EXTRA_REQUIRED: dict[int, str] = {
    1: "postcode",
    2: "email",
    3: "extra information",
    5: "extra information",
}

mcp = MCPServer(
    "parcel",
    version="0.1.0",
    instructions=(
        "Track and add parcel deliveries through the Parcel app API. "
        "Call search_carriers first when you need a carrier_code for add_delivery; "
        "codes are internal identifiers such as 'lp' (La Poste) or 'chrono' (Chronopost). "
        "Rate limits are tight: 20 delivery listings per hour, 20 additions per day."
    ),
)

_cache: dict[str, tuple[float, Any]] = {}


class ParcelError(ToolError):
    """Upstream returned success=false, or the transport failed.

    Subclasses the SDK's ToolError so the message reaches the client instead of
    being replaced by a generic "error executing tool".
    """


def _api_key() -> str:
    key = os.environ.get("PARCEL_TOKEN") or os.environ.get("PARCEL_API_KEY")
    if not key:
        raise ParcelError(
            "No API key. Set PARCEL_TOKEN (e.g. via `envchain parcel ...`); "
            "generate one at https://web.parcelapp.net."
        )
    return key.strip()


def _cached(key: str, ttl: float) -> Any | None:
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    return None


def _store(key: str, value: Any) -> Any:
    _cache[key] = (time.monotonic(), value)
    return value


def _request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """Call the Parcel API and unwrap its ``success``/``error_message`` envelope."""
    headers = {"api-key": _api_key(), "accept": "application/json"}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:  # network, DNS, timeout...
        raise ParcelError(f"Request to Parcel failed: {exc}") from exc

    if response.status_code == 401:
        raise ParcelError("Parcel rejected the API key (HTTP 401).")
    if response.status_code == 429:
        raise ParcelError(
            "Parcel rate limit hit (HTTP 429): 20 delivery listings per hour, "
            "20 additions per day."
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


def _load_carriers() -> dict[str, dict[str, Any]]:
    cached = _cached("carriers", CARRIERS_TTL)
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(CARRIERS_URL)
            response.raise_for_status()
            carriers = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ParcelError(f"Could not fetch the carrier list: {exc}") from exc
    return _store("carriers", carriers)


def _carrier_name(code: str) -> str:
    try:
        return _load_carriers().get(code, {}).get("name", code)
    except ParcelError:
        return code


@mcp.tool(
    annotations=ToolAnnotations(
        title="List deliveries", readOnlyHint=True, openWorldHint=True
    )
)
def list_deliveries(filter_mode: Literal["recent", "active"] = "recent") -> dict[str, Any]:
    """List the user's deliveries tracked in the Parcel app.

    Returns cached data from the Parcel servers; calling this does not trigger a
    carrier refresh. Rate limit: 20 requests per hour.

    Args:
        filter_mode: "active" for in-flight deliveries only, "recent" (default)
            for recent ones including completed.

    Returns:
        A dict with ``count`` and ``deliveries``. Each delivery carries the
        carrier code plus its resolved name, description, tracking number, a
        numeric ``status_code`` with a human-readable ``status``, expected dates
        when the carrier provides them, and the list of tracking ``events``
        (most useful first as returned by the carrier).
    """
    cache_key = f"deliveries:{filter_mode}"
    payload = _cached(cache_key, DELIVERIES_TTL)
    if payload is None:
        payload = _request(
            "GET",
            f"{API_BASE}/deliveries/",
            params={"filter_mode": filter_mode},
        )
        _store(cache_key, payload)

    deliveries = []
    for item in payload.get("deliveries") or []:
        enriched = dict(item)
        code = item.get("status_code")
        enriched["status"] = STATUS_CODES.get(code, f"unknown status code {code}")
        enriched["carrier_name"] = _carrier_name(item.get("carrier_code", ""))
        deliveries.append(enriched)

    return {"filter_mode": filter_mode, "count": len(deliveries), "deliveries": deliveries}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add delivery",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def add_delivery(
    tracking_number: str,
    carrier_code: str,
    description: str,
    language: str = "en",
    send_push_confirmation: bool = False,
    postcode: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Add a delivery to the user's Parcel account.

    The delivery shows "No data available" in the app until the Parcel server
    performs its first update. Rate limit: 20 requests per day, failed attempts
    included, so validate the carrier code with search_carriers beforehand.

    Args:
        tracking_number: Tracking number. Rejected upstream if its format is unknown.
        carrier_code: Internal carrier code from search_carriers. Use "pholder"
            for a placeholder delivery with no real carrier.
        description: Label shown in the app.
        language: ISO 639-1 two-letter code for carrier-provided text. Default "en".
        send_push_confirmation: Push a notification to the user's devices once added.
        postcode: Required by some carriers (those with extra_required = postcode).
        email: Required by some carriers, e.g. Apple Store orders.

    Returns:
        A dict confirming the delivery that was submitted.
    """
    body: dict[str, Any] = {
        "tracking_number": tracking_number.strip(),
        "carrier_code": carrier_code.strip(),
        "description": description.strip(),
        "language": language.strip().lower(),
        "send_push_confirmation": send_push_confirmation,
    }
    if postcode:
        body["postcode"] = postcode.strip()
    if email:
        body["email"] = email.strip()

    carriers = _load_carriers()
    if body["carrier_code"] not in carriers:
        raise ParcelError(
            f"Unknown carrier code {body['carrier_code']!r}. "
            "Use search_carriers to find the right one."
        )

    required = carriers[body["carrier_code"]].get("extra_required")
    if required == 1 and not postcode:
        raise ParcelError(
            f"{_carrier_name(body['carrier_code'])} requires a postcode; pass `postcode`."
        )
    if required == 2 and not email:
        raise ParcelError(
            f"{_carrier_name(body['carrier_code'])} requires an email; pass `email`."
        )

    _request("POST", f"{API_BASE}/add-delivery/", json=body)
    _cache.pop("deliveries:recent", None)
    _cache.pop("deliveries:active", None)

    return {
        "added": True,
        "tracking_number": body["tracking_number"],
        "carrier_code": body["carrier_code"],
        "carrier_name": _carrier_name(body["carrier_code"]),
        "description": body["description"],
        "note": "Tracking data appears only after the Parcel server's first update.",
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search carriers", readOnlyHint=True, openWorldHint=True
    )
)
def search_carriers(query: str = "", limit: int = 25) -> dict[str, Any]:
    """Find Parcel carrier codes by name or code.

    Args:
        query: Case-insensitive substring matched against carrier names and codes.
            Empty returns the first `limit` carriers alphabetically.
        limit: Maximum number of results. Default 25.

    Returns:
        A dict with ``count``, ``total`` and ``carriers``; each entry has
        ``code``, ``name`` and, when the carrier needs it, ``extra_required``
        naming the additional field add_delivery must be given.
    """
    carriers = _load_carriers()
    needle = query.strip().lower()

    matches = []
    for code, info in sorted(carriers.items(), key=lambda kv: kv[1].get("name", kv[0]).lower()):
        name = info.get("name", code)
        haystack = [code.lower(), name.lower()]
        haystack += [v.lower() for v in (info.get("name_variations") or {}).values()]
        if needle and not any(needle in h for h in haystack):
            continue
        entry: dict[str, Any] = {"code": code, "name": name}
        extra = info.get("extra_required")
        if extra is not None:
            entry["extra_required"] = EXTRA_REQUIRED.get(extra, "extra information")
        matches.append(entry)

    return {
        "query": query,
        "total": len(matches),
        "count": min(len(matches), max(limit, 0)),
        "carriers": matches[: max(limit, 0)],
    }


def main() -> None:
    """Entry point: run the server over stdio."""
    logging.basicConfig(level=os.environ.get("PARCEL_LOG_LEVEL", "INFO"))
    mcp.run()


if __name__ == "__main__":
    main()
