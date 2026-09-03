"""End-to-end stdio test: spawns the server as a subprocess and calls its tools.

Run with `envchain parcel uv run stdio_test.py`.
"""

import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp_types import CallToolResult, TextContent


def first_line_of_text(result: CallToolResult) -> str:
    """Return the first text block of a tool result.

    Args:
        result: The result of a call_tool round trip.

    Returns:
        The block's text, or a placeholder when the tool returned no text.
    """
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text
    return "(no text content)"


async def main() -> None:
    """Drive the server as a real MCP client over stdio."""
    params = StdioServerParameters(
        command="uv",
        args=["run", "--quiet", "parcelapp-mcp"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        info = await session.initialize()
        print("server:", info.server_info.name, info.server_info.version)

        tools = await session.list_tools()
        for tool in tools.tools:
            summary = (tool.description or "(no description)").splitlines()[0]
            print(f"  - {tool.name}: {summary}")

        result = await session.call_tool("search_carriers", {"query": "chronopost"})
        print("\nsearch_carriers:", result.structured_content)

        result = await session.call_tool("list_deliveries", {"filter_mode": "active"})
        print("\nlist_deliveries is_error:", result.is_error)
        print("count:", (result.structured_content or {}).get("count"))

        # Guard rails, without spending the daily add_delivery budget.
        result = await session.call_tool(
            "add_delivery",
            {
                "tracking_number": "TEST123",
                "carrier_code": "not-a-carrier",
                "description": "should fail locally",
            },
        )
        print("\nbad carrier -> is_error:", result.is_error, first_line_of_text(result))

        result = await session.call_tool(
            "add_delivery",
            {
                "tracking_number": "TEST123",
                "carrier_code": "bpost",
                "description": "missing postcode",
            },
        )
        print("missing postcode -> is_error:", result.is_error, first_line_of_text(result))


if __name__ == "__main__":
    asyncio.run(main())
