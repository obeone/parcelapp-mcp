"""Live smoke test: run with `envchain parcel uv run smoke_test.py`.

Read-only. It lists tools, searches carriers, and fetches deliveries.
It never calls add_delivery (that endpoint is capped at 20 requests per day).
"""

import asyncio
import json

from parcel_mcp.server import list_deliveries, mcp, search_carriers


async def main() -> None:
    print("tools:", [t.name for t in await mcp.list_tools()])

    print("\nsearch_carriers('la poste'):")
    print(json.dumps(search_carriers("la poste", limit=5), indent=2, ensure_ascii=False))

    for mode in ("active", "recent"):
        print(f"\nlist_deliveries({mode!r}):")
        result = list_deliveries(mode)
        print(f"  count={result['count']}")
        for d in result["deliveries"]:
            print(
                f"  - {d['description']} [{d['carrier_name']}] "
                f"{d['tracking_number']} -> {d['status']} "
                f"({len(d.get('events') or [])} events)"
            )


if __name__ == "__main__":
    asyncio.run(main())
