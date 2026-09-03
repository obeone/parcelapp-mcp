"""MCP server exposing the Parcel (parcelapp.net) external API.

Two upstream endpoints are wrapped:

* ``GET  /external/deliveries/``   - recent or active deliveries (20 req/hour)
* ``POST /external/add-delivery/`` - add one delivery          (20 req/day)

A third tool searches the public carrier-code list, since both endpoints speak
in internal carrier codes rather than human names.

This module holds the MCP surface only: the tool definitions, whose docstrings
are the contract the model reads, and the two tables that turn the API's
integers into words. All HTTP lives in :mod:`parcel_mcp.client`.

Authentication: an API key generated at https://web.parcelapp.net, read from
``PARCEL_TOKEN`` (or ``PARCEL_API_KEY``) and sent in the ``api-key`` header.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .client import (
    API_BASE,
    DELIVERIES_TTL,
    ParcelError,
    cached,
    carrier_name,
    clear_cache,
    load_carriers,
    request,
    store,
)

LOG = logging.getLogger("parcel-mcp")

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
    payload = cached(cache_key, DELIVERIES_TTL)
    if payload is None:
        payload = request(
            "GET",
            f"{API_BASE}/deliveries/",
            params={"filter_mode": filter_mode},
        )
        store(cache_key, payload)

    deliveries = []
    for item in payload.get("deliveries") or []:
        enriched = dict(item)
        code = item.get("status_code")
        enriched["status"] = STATUS_CODES.get(code, f"unknown status code {code}")
        enriched["carrier_name"] = carrier_name(item.get("carrier_code", ""))
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

    # Validate locally first: the daily budget counts failed attempts, so a
    # preventable error must never reach the network.
    carriers = load_carriers()
    if body["carrier_code"] not in carriers:
        raise ParcelError(
            f"Unknown carrier code {body['carrier_code']!r}. "
            "Use search_carriers to find the right one."
        )

    required = carriers[body["carrier_code"]].get("extra_required")
    if required == 1 and not postcode:
        raise ParcelError(
            f"{carrier_name(body['carrier_code'])} requires a postcode; pass `postcode`."
        )
    if required == 2 and not email:
        raise ParcelError(
            f"{carrier_name(body['carrier_code'])} requires an email; pass `email`."
        )

    request("POST", f"{API_BASE}/add-delivery/", json=body)
    clear_cache("deliveries:recent", "deliveries:active")

    return {
        "added": True,
        "tracking_number": body["tracking_number"],
        "carrier_code": body["carrier_code"],
        "carrier_name": carrier_name(body["carrier_code"]),
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
    carriers = load_carriers()
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
