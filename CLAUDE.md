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
uv sync                                       # install deps and the dev group
uv run pytest                                 # the whole suite, no network
uv run pytest tests/test_tools.py::test_add_delivery_demands_a_postcode_without_spending_a_request
uv run ruff check && uv run ruff format       # lint, then format
uv run mypy                                   # strict, and currently clean
envchain parcel uv run parcelapp-mcp          # run the server over stdio
envchain parcel uv run scripts/smoke_test.py  # live, read-only check (in-process)
envchain parcel uv run scripts/stdio_test.py  # live, end-to-end MCP client over stdio
```

`uv run pytest` is the loop to work in: it never touches the network, so it costs
nothing from either rate-limit budget. The two `scripts/` entries do hit the real API
and are manual checks, not part of the suite.

The suite mocks the upstream with `respx`. Every test runs under `@respx.mock`, so an
unmocked request fails the test rather than escaping to the real API.

CI (`.github/workflows/ci.yml`) runs those same three commands: lint and types once,
tests across 3.10 to 3.13. It uses `uv sync --locked`, so a dependency change means
committing the refreshed `uv.lock` alongside it.

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

Two modules, split along one line: `client.py` knows nothing about MCP, `server.py`
knows nothing about HTTP. Resist adding a third for three tools.

`src/parcel_mcp/client.py` — the network:

- `request()` is the single HTTP chokepoint. It injects the `api-key` header,
  maps 401 and 429 to explicit messages, and unwraps the upstream
  `{"success": bool, "error_message": str}` envelope. Every upstream failure mode is
  normalised here, so tools never inspect status codes themselves.
- `_cache` is a module-level `dict[str, tuple[float, Any]]`, with per-call TTLs via
  `cached()` / `store()` and eviction via `clear_cache()`. Deliveries: 180 s (upstream
  serves a cached view anyway, so this costs no freshness and protects the hourly
  budget). Carriers: 24 h. It is process-global state, so tests must call
  `clear_cache()` between cases.
- `ParcelError` lives here, next to what raises it, though it reaches into the SDK.
  See the invariant below.

`src/parcel_mcp/server.py` — the MCP surface:

- The `MCPServer` instance, the three tools, `main()`.
- The two translation tables, `STATUS_CODES` and `EXTRA_REQUIRED`, exist because the
  upstream API speaks in integers. Resolving them, along with carrier codes to names,
  is the entire value this server adds. Do not strip it in favour of raw passthrough.
- `add_delivery`'s local validation stays here, beside the error messages it produces.

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
- `carrier_name()` swallows `ParcelError` and falls back to the raw code, so a
  carrier-list fetch failure degrades `list_deliveries` instead of breaking it.
- **`@mcp.tool()` returns the function unchanged.** It registers and hands back `fn`,
  so tests and scripts can call `list_deliveries(...)` directly without going through
  `mcp.call_tool`.
- **`ToolAnnotations` fields are snake_case in Python.** `read_only_hint`, not
  `readOnlyHint`. The camelCase spelling is the wire alias; pydantic accepts it, but
  mypy cannot check it. The serialised JSON is identical either way.

## Auth

`PARCEL_TOKEN`, falling back to `PARCEL_API_KEY`, sent as the `api-key` header. Keys
are generated at <https://web.parcelapp.net>. Never put a realistic-looking key in a
test fixture.

## Dependencies

Standard library plus `httpx` and `mcp`. Justify any third dependency before adding
it; `respx` or `httpx.MockTransport` for the future test suite is the expected
exception.
