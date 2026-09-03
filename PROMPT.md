# Kickoff prompt — parcelapp-mcp

Paste everything below into Claude Code from the repo root.

---

You are working on `parcelapp-mcp`, an MCP server wrapping the
[Parcel](https://parcelapp.net) delivery-tracking API. A working prototype already
exists in this repo — three tools, live-tested against the real API. Your job is to
turn it into a publishable open-source project without breaking what works.

## What the project is

Parcel is a macOS/iOS delivery-tracking app. Premium users get a small external API,
documented at:

- <https://parcelapp.net/help/api-view-deliveries.html>
- <https://parcelapp.net/help/api-add-delivery.html>

This server exposes that API to MCP clients (Claude Desktop, Claude Code, Home
Assistant-style automations). It is deliberately thin: the upstream API is small,
read-mostly, and heavily rate-limited. The value we add is not abstraction — it is
making the API *legible to a model*: internal carrier codes resolved to names,
numeric status codes resolved to text, required extra fields validated before a
request is spent.

## Current state

```
parcel_mcp/server.py   # everything: three tools, HTTP client, caches
smoke_test.py          # in-process read-only check
stdio_test.py          # end-to-end client over stdio
pyproject.toml         # hatchling, entry point `parcel-mcp`
README.md
```

Tools:

| Tool | Upstream limit |
| --- | --- |
| `list_deliveries(filter_mode)` — recent/active, statuses and carriers resolved | 20 / hour |
| `add_delivery(...)` — one delivery, validated locally first | 20 / **day**, failures included |
| `search_carriers(query, limit)` — find carrier codes, flag postcode/email requirements | none (cached 24 h) |

Auth: `PARCEL_TOKEN` (fallback `PARCEL_API_KEY`), sent as the `api-key` header. Locally
it comes from `envchain parcel`.

## Design decisions to preserve

These were deliberate. Change them only with a reason, and say what it is.

1. **The rate limits drive the design.** 20 additions per day, failed attempts
   included. `add_delivery` therefore validates the carrier code and any required
   postcode/email *locally* before touching the network. Never let a preventable
   error consume a request.
2. **`ParcelError` subclasses the SDK's `ToolError`.** Without that, mcp 2.x replaces
   the message with a generic "Error executing tool" and the model learns nothing.
3. **mcp 2.x API** (`MCPServer`, not `FastMCP`). The v1 spelling is gone; don't
   reintroduce it from memory.
4. **Local caching** — 3 min for deliveries (upstream serves cached data anyway),
   24 h for the carrier list. It protects the hourly budget at zero cost in freshness.
5. **Docstrings are the tool contracts.** They are what the model reads. Keep them
   describing behaviour, arguments and limits — not implementation.
6. **Errors name the fix.** "Bpost requires a postcode; pass `postcode`" beats
   "invalid request".

## What to do

Work through these in order, committing each as a separate, reviewable change.
Ask before anything destructive or anything that pushes to a remote.

1. **Restructure for a public repo.** `src/parcel_mcp/` layout, tests under `tests/`.
   Split `server.py` into a small client module and a tools module if it stays
   readable — do not over-engineer three tools into a framework.
2. **Real test suite.** pytest, with `respx` or `httpx.MockTransport` faking the
   upstream. Cover: the success envelope, `success: false` with `error_message`,
   HTTP 401 and 429, cache hit and expiry, carrier validation refusing a bad code,
   and the missing postcode/email guards. No network in CI. Keep the two live
   scripts as manual smoke tests, moved to `scripts/`.
3. **Tooling.** `ruff` (lint + format), `mypy --strict` if it doesn't fight the SDK,
   `uv.lock` committed, a GitHub Actions workflow running lint and tests on 3.10–3.13.
4. **Packaging and docs.** MIT licence, a README that opens with what it does and how
   to install it, `uvx parcelapp-mcp` usable straight from git, Claude Desktop and
   Claude Code config snippets, and an honest limitations section.
5. **Then, and only then, features.** Candidates, in rough order of value:
   - an MCP resource (`parcel://deliveries`) for passive consultation without a tool call
   - server-side filtering/sorting in `list_deliveries` (by status, by expected date)
   - surfacing remaining rate-limit budget in tool results so the model can pace itself
   - `--transport streamable-http` for non-stdio clients

## Constraints

- Python. Type hints throughout. Standard library plus `httpx` and `mcp`; justify any
  third dependency.
- Code, comments, commit messages and docs in English.
- Do not commit an API key, and do not put one in a test fixture that looks real.
- `add_delivery` costs one of 20 daily requests. Never call it live during development
  unless explicitly asked; test it against a mock.
- The upstream API is undocumented beyond those two help pages. When behaviour is
  unclear, handle it defensively and note the uncertainty in a comment rather than
  guessing at a contract.

Start by reading `parcel_mcp/server.py` in full, then propose your restructuring plan
before touching anything.
