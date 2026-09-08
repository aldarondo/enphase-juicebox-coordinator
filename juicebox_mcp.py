"""
JuiceBox MCP client.

Connects to the JuiceBox MCP SSE server and calls set_charging_schedule.
Uses the MCP Python SDK client so the coordinator talks to the JuiceBox
through the same tool interface Claude uses — no back-channel coupling.
"""

import json
import logging
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

from errors import surfacing_errors

log = logging.getLogger(__name__)

JUICEBOX_MCP_URL = os.getenv("JUICEBOX_MCP_URL", "http://<YOUR-NAS-IP>:3001/sse")


async def _call_tool(tool_name: str, args: dict | None = None):
    """Open an SSE session and call one tool, returning the raw CallToolResult.

    Wrapped in ``surfacing_errors`` because the MCP SSE transport runs inside an
    anyio task group: without it a connection failure surfaces as the opaque
    "unhandled errors in a TaskGroup (1 sub-exception)" instead of the real cause.
    """
    async with surfacing_errors(tool_name):
        async with sse_client(JUICEBOX_MCP_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool_name, args or {})


def _parse_content(result, default: dict) -> dict:
    """Return the first content item parsed as JSON, else a raw/empty fallback."""
    if not result.content:
        return default
    text = result.content[0].text
    try:
        return json.loads(text)
    except (ValueError, AttributeError):
        return {"raw": text} if text else default


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
    log.info("[juicebox_mcp] Calling set_charging_schedule with %d window(s)", len(schedule))
    result = await _call_tool("set_charging_schedule", {"schedule": schedule})
    return _parse_content(result, {"success": True})


async def get_charger_status() -> dict:
    """Fetch current charger state from the JuiceBox MCP (for reporting)."""
    result = await _call_tool("get_charger_status")
    return _parse_content(result, {})
