"""End-to-end check of the streamable-http transport, against a running server.

Unlike the stdio script, this one sends the API key per request, the way a
shared deployment works. Start a server first, then point this at it:

    envchain parcel uv run parcelapp-mcp --transport streamable-http --port 8000
    envchain parcel uv run scripts/http_test.py

Set PARCEL_MCP_URL to reach a server somewhere else, such as a container.

Read-only: it never calls add_delivery, which is capped at 20 requests per day
upstream including failures.
"""

import asyncio
import os
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextResourceContents

URL = os.environ.get("PARCEL_MCP_URL", "http://127.0.0.1:8000/mcp")


async def main() -> None:
    """Drive the HTTP server as a real MCP client, with a per-request key."""
    token = os.environ.get("PARCEL_TOKEN") or os.environ.get("PARCEL_API_KEY")
    if not token:
        sys.exit("Set PARCEL_TOKEN, e.g. `envchain parcel uv run scripts/http_test.py`.")

    # The key travels with the request, not with the server: this is what lets
    # one deployment serve several people without holding anyone's credential.
    http = httpx2.AsyncClient(headers={"X-Parcel-Token": token})
    async with (
        http,
        streamable_http_client(URL, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        info = await session.initialize()
        print("server:", info.server_info.name, info.server_info.version)

        tools = await session.list_tools()
        print("tools:", [tool.name for tool in tools.tools])

        templates = await session.list_resource_templates()
        items = getattr(templates, "resource_templates", None) or []
        print("resources:", [getattr(item, "uri_template", "?") for item in items])

        result = await session.call_tool("list_deliveries", {"filter_mode": "active"})
        payload = result.structured_content or {}
        print("\nlist_deliveries is_error:", result.is_error)
        print("count:", payload.get("count"))
        print("cache:", payload.get("cache"))
        print("rate_limit:", payload.get("rate_limit"))

        resource = await session.read_resource("parcel://deliveries/active")
        block = resource.contents[0]
        size = len(block.text) if isinstance(block, TextResourceContents) else 0
        print("\nresource characters:", size)


if __name__ == "__main__":
    asyncio.run(main())
