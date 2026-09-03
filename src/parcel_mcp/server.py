"""MCP server exposing the Parcel (parcelapp.net) external API.

Two upstream endpoints are wrapped:

* ``GET  /external/deliveries/``   - recent or active deliveries (20 req/hour)
* ``POST /external/add-delivery/`` - add one delivery          (20 req/day)

A third tool searches the public carrier-code list, since both endpoints speak
in internal carrier codes rather than human names, and a
``parcel://deliveries/{filter_mode}`` resource exposes the same listing for
passive reading.

This module holds the MCP surface only: the tool definitions, whose docstrings
are the contract the model reads, and the two tables that turn the API's
integers into words. All HTTP lives in :mod:`parcel_mcp.client`.

Authentication follows the transport. Over stdio the key comes from
``PARCEL_TOKEN`` (or ``PARCEL_API_KEY``). Over HTTP each request carries its
own, in an ``X-Parcel-Token`` or ``Authorization: Bearer`` header, so one
deployment can serve several people without holding anyone's credential.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations

from .client import (
    ADD_BUCKET,
    API_BASE,
    DELIVERIES_BUCKET,
    DELIVERIES_TTL,
    ParcelError,
    api_key,
    budget,
    cache_age,
    cached,
    carrier_name,
    clear_cache,
    key_fingerprint,
    load_carriers,
    record_request,
    request,
    store,
)

LOG = logging.getLogger("parcelapp-mcp")

try:
    # One source of truth for the version: pyproject, through the installed
    # metadata. Hard-coding it here is how the two drift apart.
    VERSION = version("parcelapp-mcp")
except PackageNotFoundError:  # running straight from a source tree
    VERSION = "0.0.0+unknown"

# Headers the key may arrive in over HTTP, most explicit first.
TOKEN_HEADER = "x-parcel-token"
AUTH_HEADER = "authorization"
BEARER_PREFIX = "bearer "

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
    version=VERSION,
    instructions=(
        "Track and add parcel deliveries through the Parcel app API. "
        "Call search_carriers first when you need a carrier_code for add_delivery; "
        "codes are internal identifiers such as 'lp' (La Poste) or 'chrono' (Chronopost). "
        "Rate limits are tight: 20 delivery listings per hour, 20 additions per day. "
        "Every tool result carries a rate_limit block; read it and pace accordingly."
    ),
)


def resolve_api_key(ctx: Context | None) -> str:
    """Find the API key to use for the request being served.

    Over HTTP the key belongs to the caller, not to the server, so a header
    wins over the environment. Header values are client-supplied input and are
    treated as an opaque credential, never as an identity claim.

    Args:
        ctx: The request context, injected by the SDK. None when a tool is
            called directly, as the tests and the smoke script do.

    Returns:
        The caller's key, stripped.

    Raises:
        ParcelError: No header carried one and the environment has none either.
    """
    headers = ctx.headers if ctx is not None else None
    if headers:
        # Normalise: starlette's mapping is case-insensitive but the annotation
        # only promises a plain Mapping, and a bare dict would not be.
        lowered = {name.lower(): value for name, value in headers.items()}
        token = lowered.get(TOKEN_HEADER, "").strip()
        if token:
            return token
        auth = lowered.get(AUTH_HEADER, "")
        if auth.lower().startswith(BEARER_PREFIX):
            bearer = auth[len(BEARER_PREFIX) :].strip()
            if bearer:
                return bearer
    return api_key()


def _fetch_deliveries(filter_mode: str, key: str) -> tuple[list[dict[str, Any]], float | None]:
    """Fetch and enrich a delivery listing, through the per-caller cache.

    Args:
        filter_mode: "recent" or "active", passed straight to the API.
        key: The caller's API key.

    Returns:
        The enriched deliveries, and the age in seconds of the cached response
        they came from, or None when the fetch was live.
    """
    # Namespaced by key fingerprint: over HTTP several callers share this
    # process, and one caller's parcels must never reach another.
    fingerprint = key_fingerprint(key)
    cache_key = f"deliveries:{fingerprint}:{filter_mode}"
    payload = cached(cache_key, DELIVERIES_TTL)
    age = cache_age(cache_key) if payload is not None else None
    if payload is None:
        payload = request(
            "GET",
            f"{API_BASE}/deliveries/",
            key,
            params={"filter_mode": filter_mode},
        )
        record_request(DELIVERIES_BUCKET, fingerprint)
        store(cache_key, payload)

    deliveries = []
    for item in payload.get("deliveries") or []:
        enriched = dict(item)
        code = item.get("status_code")
        enriched["status"] = STATUS_CODES.get(code, f"unknown status code {code}")
        enriched["carrier_name"] = carrier_name(item.get("carrier_code", ""))
        deliveries.append(enriched)
    return deliveries, age


@mcp.tool(
    annotations=ToolAnnotations(title="List deliveries", read_only_hint=True, open_world_hint=True)
)
def list_deliveries(  # noqa: D417  # ctx is SDK-injected, not part of the model-facing contract
    filter_mode: Literal["recent", "active"] = "recent",
    status: str | None = None,
    sort_by: Literal["expected", "status", "description"] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List the user's deliveries tracked in the Parcel app.

    Returns cached data from the Parcel servers; calling this does not trigger a
    carrier refresh. Rate limit: 20 requests per hour, and the result says how
    much of that this server has spent.

    Filtering and sorting happen after the fetch, so narrowing the result costs
    no extra request. A local 3-minute cache means repeated calls are usually
    free.

    Args:
        filter_mode: "active" for in-flight deliveries only, "recent" (default)
            for recent ones including completed.
        status: Keep only deliveries whose resolved status contains this text,
            case-insensitive, e.g. "transit" or "out for delivery".
        sort_by: "expected" orders by expected delivery date, soonest first,
            with undated deliveries last; "status" and "description" order
            alphabetically. Unsorted by default, preserving the carrier's order.

    Returns:
        A dict with ``count``, ``deliveries``, a ``cache`` block saying whether
        the data was served locally and how old it is, and a ``rate_limit``
        block. Each delivery carries the carrier code plus its resolved name,
        description, tracking number, a numeric ``status_code`` with a
        human-readable ``status``, expected dates when the carrier provides
        them, and the list of tracking ``events``.
    """
    key = resolve_api_key(ctx)
    deliveries, age = _fetch_deliveries(filter_mode, key)
    total = len(deliveries)

    if status:
        needle = status.strip().lower()
        deliveries = [d for d in deliveries if needle in d["status"].lower()]

    if sort_by == "expected":
        # Undated deliveries sort last: an absent date is not an early one.
        deliveries.sort(
            key=lambda d: (d.get("date_expected") is None, str(d.get("date_expected") or ""))
        )
    elif sort_by == "status":
        deliveries.sort(key=lambda d: d["status"])
    elif sort_by == "description":
        deliveries.sort(key=lambda d: str(d.get("description", "")).lower())

    return {
        "filter_mode": filter_mode,
        "count": len(deliveries),
        "total_before_filter": total,
        "deliveries": deliveries,
        "cache": {
            "served_from_cache": age is not None,
            "age_seconds": None if age is None else round(age, 1),
            "ttl_seconds": DELIVERIES_TTL,
        },
        "rate_limit": budget(DELIVERIES_BUCKET, key_fingerprint(key)),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add delivery",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
def add_delivery(  # noqa: D417  # ctx is SDK-injected, not part of the model-facing contract
    tracking_number: str,
    carrier_code: str,
    description: str,
    language: str = "en",
    send_push_confirmation: bool = False,
    postcode: str | None = None,
    email: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Add a delivery to the user's Parcel account.

    The delivery shows "No data available" in the app until the Parcel server
    performs its first update. Rate limit: 20 requests per day, failed attempts
    included, so validate the carrier code with search_carriers beforehand. The
    result reports how much of the daily budget this server has spent.

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
        A dict confirming the delivery that was submitted, with a ``rate_limit``
        block for the daily budget.
    """
    key = resolve_api_key(ctx)
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
        raise ParcelError(f"{carrier_name(body['carrier_code'])} requires an email; pass `email`.")

    fingerprint = key_fingerprint(key)
    request("POST", f"{API_BASE}/add-delivery/", key, json=body)
    record_request(ADD_BUCKET, fingerprint)
    clear_cache(f"deliveries:{fingerprint}:recent", f"deliveries:{fingerprint}:active")

    return {
        "added": True,
        "tracking_number": body["tracking_number"],
        "carrier_code": body["carrier_code"],
        "carrier_name": carrier_name(body["carrier_code"]),
        "description": body["description"],
        "note": "Tracking data appears only after the Parcel server's first update.",
        "rate_limit": budget(ADD_BUCKET, fingerprint),
    }


@mcp.tool(
    annotations=ToolAnnotations(title="Search carriers", read_only_hint=True, open_world_hint=True)
)
def search_carriers(query: str = "", limit: int = 25) -> dict[str, Any]:
    """Find Parcel carrier codes by name or code.

    The carrier catalogue is public and not rate-limited, so this tool is free
    to call and its results are cached for 24 hours.

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


@mcp.resource(
    "parcel://deliveries/{filter_mode}",
    name="Deliveries",
    description=(
        "The user's deliveries, for reading without a tool call. "
        "Use parcel://deliveries/active or parcel://deliveries/recent."
    ),
    mime_type="application/json",
)
def deliveries_resource(filter_mode: str, ctx: Context | None = None) -> str:
    """Expose the deliveries as a readable resource.

    A URI template rather than a static resource for two reasons: it carries
    the filter choice, and the SDK only injects a Context into templated
    resources, which is what lets this read a per-request key over HTTP.

    Shares the cache and the hourly budget with list_deliveries, so consulting
    the resource cannot quietly drain the allowance behind the tool's back.

    Args:
        filter_mode: "active" or "recent". Anything else is rejected rather
            than forwarded, since the API's behaviour on other values is
            undocumented.
        ctx: The request context, injected by the SDK.

    Returns:
        A JSON document with the deliveries and the same rate_limit block the
        tool returns.

    Raises:
        ParcelError: ``filter_mode`` was neither "active" nor "recent".
    """
    if filter_mode not in ("active", "recent"):
        raise ParcelError(
            f"Unknown filter_mode {filter_mode!r}. "
            "Use parcel://deliveries/active or parcel://deliveries/recent."
        )
    key = resolve_api_key(ctx)
    deliveries, age = _fetch_deliveries(filter_mode, key)
    return json.dumps(
        {
            "filter_mode": filter_mode,
            "count": len(deliveries),
            "deliveries": deliveries,
            "cache": {
                "served_from_cache": age is not None,
                "age_seconds": None if age is None else round(age, 1),
            },
            "rate_limit": budget(DELIVERIES_BUCKET, key_fingerprint(key)),
        },
        indent=2,
        ensure_ascii=False,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        A parser for the transport options. stdio stays the default, so an
        existing client configuration keeps working untouched.
    """
    parser = argparse.ArgumentParser(
        prog="parcelapp-mcp",
        description="MCP server for the Parcel delivery tracking API.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("PARCEL_TRANSPORT", "stdio"),
        help="Transport to serve on. Default: stdio.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PARCEL_HOST", "127.0.0.1"),
        help="Bind address for streamable-http. Default: 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PARCEL_PORT", "8000")),
        help="Port for streamable-http. Default: 8000.",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("PARCEL_PATH", "/mcp"),
        help="URL path for streamable-http. Default: /mcp.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point.

    Args:
        argv: Command-line arguments, defaulting to sys.argv.
    """
    logging.basicConfig(level=os.environ.get("PARCEL_LOG_LEVEL", "INFO"))
    args = build_parser().parse_args(argv)

    if args.transport == "stdio":
        mcp.run()
        return

    LOG.info("serving streamable-http on http://%s:%s%s", args.host, args.port, args.path)
    LOG.info("clients must send their Parcel key as X-Parcel-Token or Authorization: Bearer")
    # Stateless: nothing here is worth a session, and it makes the server
    # trivially replaceable behind a load balancer or restarted at will.
    mcp.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path=args.path,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
