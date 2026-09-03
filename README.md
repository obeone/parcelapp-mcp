# parcel-mcp

MCP server for the [Parcel](https://parcelapp.net) delivery tracking app. Wraps the
two external API endpoints available to premium users, plus a carrier-code lookup.

## Tools

| Tool | Purpose | Upstream rate limit |
| --- | --- | --- |
| `list_deliveries(filter_mode)` | Recent or active deliveries, with status codes resolved to text and carrier codes to names | 20 / hour |
| `add_delivery(...)` | Add one delivery to the account | 20 / day, failures included |
| `search_carriers(query, limit)` | Find the internal `carrier_code` for a carrier, and whether it needs a postcode or email | none (cached 24 h) |

`list_deliveries` results are cached locally for 3 minutes — the API serves a cached
response anyway, so this only protects the hourly budget.

## Authentication

Generate an API key at [web.parcelapp.net](https://web.parcelapp.net) and expose it as
`PARCEL_TOKEN` (`PARCEL_API_KEY` also works). It is sent in the `api-key` header.

```bash
envchain --set parcel PARCEL_TOKEN
```

## Run

```bash
envchain parcel uv run --directory /path/to/parcel-mcp parcel-mcp
```

## Claude Desktop / Claude Code config

```json
{
  "mcpServers": {
    "parcel": {
      "command": "envchain",
      "args": ["parcel", "uv", "run", "--directory", "/path/to/parcel-mcp", "parcel-mcp"]
    }
  }
}
```

Without envchain, drop the `envchain parcel` prefix and pass the key through `env`:

```json
{
  "mcpServers": {
    "parcel": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/parcel-mcp", "parcel-mcp"],
      "env": { "PARCEL_TOKEN": "..." }
    }
  }
}
```

## Notes and limitations

- Reading deliveries never triggers a carrier refresh; you always get the app
  server's cached view.
- A newly added delivery shows "No data available" until the server's first update.
- One delivery per `add_delivery` call, and the tracking number must match a format
  the server recognises. Use `carrier_code: "pholder"` for a placeholder delivery.
- Some carriers require an extra field (postcode or email). `search_carriers` reports
  which, and `add_delivery` refuses to burn a daily request when it is missing.

## Smoke test

Read-only; never calls `add_delivery`.

```bash
envchain parcel uv run smoke_test.py
```
