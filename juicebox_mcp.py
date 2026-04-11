"""
JuiceBox MCP client.

Connects to the JuiceBox MCP SSE server and calls set_charging_schedule.
Uses the MCP Python SDK client so the coordinator talks to the JuiceBox
through the same tool interface Claude uses — no back-channel coupling.
"""

import os
import logging
from mcp import ClientSession
from mcp.client.sse import sse_client

log = logging.getLogger(__name__)

JUICEBOX_MCP_URL = os.getenv("JUICEBOX_MCP_URL", "http://192.168.0.64:3001/sse")


async def set_charging_schedule(schedule: list[dict]) -> dict:
    """
    Call the JuiceBox MCP's set_charging_schedule tool.

    Args:
        schedule: List of charging window dicts (label, days, start, end, max_amps).
                  Pass [] to clear all scheduled charging.

    Returns:
        The tool's response as a dict.

    Raises:
        Exception if the MCP server is unreachable or the tool call fails.
    """
    log.info("[juicebox_mcp] Connecting to %s", JUICEBOX_MCP_URL)
    async with sse_client(JUICEBOX_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("[juicebox_mcp] Calling set_charging_schedule with %d window(s)", len(schedule))
            result = await session.call_tool("set_charging_schedule", {"schedule": schedule})
            # Result content is a list of TextContent; return the parsed first item
            if result.content:
                import json
                text = result.content[0].text
                try:
                    return json.loads(text)
                except (ValueError, AttributeError):
                    return {"raw": text}
            return {"success": True}


async def get_charger_status() -> dict:
    """Fetch current charger state from the JuiceBox MCP (for reporting)."""
    async with sse_client(JUICEBOX_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_charger_status", {})
            if result.content:
                import json
                try:
                    return json.loads(result.content[0].text)
                except (ValueError, AttributeError):
                    pass
    return {}
