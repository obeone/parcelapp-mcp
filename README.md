# parcelapp-mcp

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
envchain parcel uv run --directory /path/to/parcelapp-mcp parcelapp-mcp
```

## Claude Desktop / Claude Code config

```json
{
  "mcpServers": {
    "parcel": {
      "command": "envchain",
      "args": ["parcel", "uv", "run", "--directory", "/path/to/parcelapp-mcp", "parcelapp-mcp"]
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
      "args": ["run", "--directory", "/path/to/parcelapp-mcp", "parcelapp-mcp"],
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

## Development

```bash
uv sync
uv run pytest
```

The suite mocks the upstream with `respx` and never touches the network, so it costs
nothing from either rate-limit budget.

Two live scripts remain as manual checks. Both are read-only: neither calls
`add_delivery`, which is capped at 20 requests per day including failures.

```bash
envchain parcel uv run scripts/smoke_test.py   # in-process
envchain parcel uv run scripts/stdio_test.py   # through a real MCP client
```
