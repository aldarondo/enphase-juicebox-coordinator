"""
End-to-end test — calls run_coordinator on the deployed NAS stack.

Connects to the coordinator MCP server at COORDINATOR_URL, triggers
run_coordinator, and asserts the result is not an error.

Usage:
    python scripts/e2e_test.py [--url http://<YOUR-NAS-IP>:8767/sse]
"""

import argparse
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client


COORDINATOR_URL = "http://<YOUR-NAS-IP>:8767/sse"


async def run_e2e(url: str) -> int:
    print(f"Connecting to coordinator MCP at {url} ...")

    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            print(f"Available tools: {tool_names}")
            assert "run_coordinator" in tool_names, "run_coordinator tool not found"

            print("\nCalling run_coordinator ...")
            result = await session.call_tool("run_coordinator", {})

            raw = result.content[0].text if result.content else "{}"
            data = json.loads(raw)

            print("\n-- Result --------------------------------------------------")
            print(json.dumps(data, indent=2))
            print("------------------------------------------------------------\n")

            status = data.get("status", "unknown")
            errors = data.get("errors", [])
            juicebox_ok = data.get("juicebox_ok", False)

            if status == "ok":
                print(f"[PASS] status=ok, juicebox_ok={juicebox_ok}")
                return 0
            elif status == "partial":
                print(f"[WARN] status=partial — JuiceBox updated but some errors:")
                for e in errors:
                    print(f"  • {e}")
                return 0
            else:
                print(f"[FAIL] status={status}")
                for e in errors:
                    print(f"  • {e}")
                return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=COORDINATOR_URL)
    args = parser.parse_args()

    exit_code = asyncio.run(run_e2e(args.url))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
