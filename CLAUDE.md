# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server wrapping the [Parcel](https://parcelapp.net) delivery-tracking API,
which premium users of the macOS/iOS app get access to. Two upstream endpoints plus a
public carrier-code list, exposed as three MCP tools.

The upstream API is documented only by two help pages:

- <https://parcelapp.net/help/api-view-deliveries.html>
- <https://parcelapp.net/help/api-add-delivery.html>

Anything beyond those pages is undocumented. When behaviour is unclear, handle it
defensively and note the uncertainty in a comment rather than inventing a contract.

`PROMPT.md` holds the original project brief (restructuring roadmap and feature
backlog). Read it before starting work that touches project layout or scope.

## Commands

The project uses `uv`. The API key comes from `envchain parcel` locally.

```bash
uv sync                                       # install deps into .venv
envchain parcel uv run parcelapp-mcp          # run the server over stdio
envchain parcel uv run scripts/smoke_test.py  # live, read-only check (in-process)
envchain parcel uv run scripts/stdio_test.py  # live, end-to-end MCP client over stdio
```

There is no pytest suite, ruff config, or CI yet; adding them is item 2 and 3 of
`PROMPT.md`. Once pytest exists, a single test runs with
`uv run pytest tests/test_x.py::test_name`.

## Rate limits drive everything

| Endpoint | Limit |
| --- | --- |
| `deliveries/` | 20 per hour |
| `add-delivery/` | 20 per **day**, failed attempts included |
| `supported_carriers.json` | none |

Two consequences the code depends on:

- **`add_delivery` validates locally before touching the network.** It checks the
  carrier code against the cached carrier list and enforces the carrier's
  `extra_required` field (postcode or email) *before* issuing the POST. A
  preventable error must never consume one of the 20 daily requests.
- **`add_delivery` must not be called live during development.** Test it against a
  mock. `smoke_test.py` deliberately never touches it; `stdio_test.py` only exercises
  the local guard rails, which fail before any request is sent.

## Architecture

Everything currently lives in `src/parcel_mcp/server.py` (~330 lines):

- `_request()` is the single HTTP chokepoint. It injects the `api-key` header,
  maps 401 and 429 to explicit messages, and unwraps the upstream
  `{"success": bool, "error_message": str}` envelope. Every upstream failure mode is
  normalised here, so tools never inspect status codes themselves.
- `_cache` is a module-level `dict[str, tuple[float, Any]]` keyed by a string, with
  per-call TTLs via `_cached()` / `_store()`. Deliveries: 180 s (upstream serves a
  cached view anyway, so this costs no freshness and protects the hourly budget).
  Carriers: 24 h. `add_delivery` invalidates both delivery cache keys on success.
- The two translation tables, `STATUS_CODES` and `EXTRA_REQUIRED`, exist because the
  upstream API speaks in integers. Resolving them, along with carrier codes to names,
  is the entire value this server adds. Do not strip it in favour of raw passthrough.

### Non-obvious invariants

- **`ParcelError` subclasses the SDK's `ToolError`.** If it does not, mcp 2.x
  replaces the message with a generic "Error executing tool" and the model learns
  nothing. Do not reparent it to `Exception`.
- **mcp 2.x API: `MCPServer`, not `FastMCP`.** The v1 spelling is gone. Do not
  reintroduce it from memory; check the installed version (`mcp` 2.1.x) when unsure.
- **Docstrings are the tool contracts.** They are the text the model actually reads.
  Keep them describing behaviour, arguments and rate limits, not implementation.
- **Errors name the fix.** "Bpost requires a postcode; pass `postcode`" beats
  "invalid request". Any new error path follows that shape.
- `_carrier_name()` swallows `ParcelError` and falls back to the raw code, so a
  carrier-list fetch failure degrades `list_deliveries` instead of breaking it.

## Auth

`PARCEL_TOKEN`, falling back to `PARCEL_API_KEY`, sent as the `api-key` header. Keys
are generated at <https://web.parcelapp.net>. Never put a realistic-looking key in a
test fixture.

## Dependencies

Standard library plus `httpx` and `mcp`. Justify any third dependency before adding
it; `respx` or `httpx.MockTransport` for the future test suite is the expected
exception.
