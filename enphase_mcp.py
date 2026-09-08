"""
Enphase MCP client.

Connects to the claude-enphase MCP SSE server and calls its tools.
Mirrors juicebox_mcp.py — the coordinator talks to both upstream MCPs the same way.
"""

import json
import logging
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

from errors import surfacing_errors

log = logging.getLogger(__name__)

ENPHASE_MCP_URL = os.getenv("ENPHASE_MCP_URL", "http://<YOUR-NAS-IP>:8766/sse")


def _raise_if_error_payload(data, tool_name: str) -> None:
    """Raise RuntimeError if a parsed JSON payload actually represents an error.

    claude-enphase doesn't always signal failure via ``result.isError`` or an
    ``"Error:"`` text prefix — when an upstream call fails (DNS failure, expired
    token, Enphase 5xx) it can return a structured error dict such as
    ``{"error": "token expired"}`` or ``{"status": "error", "message": "..."}``.

    Left unhandled, that dict flows back as a normal value, ``_extract_mode``
    in battery_mode.py returns ``None``, and a mode switch reports the misleading
    "Enphase did not confirm target mode (got None, ...)" instead of the real
    cause. Surfacing it here puts the actual error in retry logs and alert emails.
    """
    if not isinstance(data, dict):
        return
    status = data.get("status")
    is_error_status = isinstance(status, str) and status.lower() in ("error", "failed", "failure")
    err = data.get("error")
    if is_error_status or err:
        detail = (
            data.get("message")
            or (err if isinstance(err, str) else None)
            or data.get("detail")
            or json.dumps(data)
        )
        raise RuntimeError(f"{tool_name} failed: {detail}")


def _parse_tool_result(result, tool_name: str) -> dict:
    """Parse an MCP CallToolResult into a dict, raising on every error shape.

    Failure is signalled three ways, all of which must surface as an exception:
      1. ``result.isError`` / empty content        → MCP-level error
      2. text starting with ``"Error:"``           → server's plain-text convention
      3. a JSON error dict                          → structured error response
    """
    if result.isError or not result.content:
        text = result.content[0].text if result.content else "empty response"
        raise RuntimeError(f"{tool_name} failed: {text}")
    text = result.content[0].text
    if text.startswith("Error:"):
        raise RuntimeError(f"{tool_name} failed: {text}")
    try:
        data = json.loads(text)
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"{tool_name} returned non-JSON: {text[:200]}") from exc
    _raise_if_error_payload(data, tool_name)
    return data


async def _call_tool(tool_name: str, args: dict | None = None) -> dict:
    """Open an SSE session, call one tool, and parse the result.

    The ``surfacing_errors`` wrapper matters here: ``sse_client`` and
    ``ClientSession`` run their transports in anyio task groups, so a connection
    failure escapes as a ``BaseExceptionGroup`` whose message is the useless
    "unhandled errors in a TaskGroup (1 sub-exception)". Unwrapping it means the
    retry logs and alert emails name the actual cause instead.

    Parsing deliberately happens after the connection blocks have exited, so a
    parse error is never re-wrapped by the task group on the way out.
    """
    async with surfacing_errors(tool_name):
        async with sse_client(ENPHASE_MCP_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args or {})
    return _parse_tool_result(result, tool_name)


async def _call_tool_flag(tool_name: str, key: str = "active") -> bool:
    """Call a tool that returns a boolean flag, failing open (False) on any error.

    Used for the advisory checks — a Storm Guard or grid-event lookup that can't
    complete must never block a mode switch.
    """
    try:
        data = await _call_tool(tool_name)
    except Exception as exc:
        log.warning("[enphase_mcp] %s failed (failing open): %s", tool_name, exc)
        return False
    return bool(data.get(key, False))


async def get_energy_summary(date_str: str | None = None) -> dict:
    """
    Call enphase_get_energy_summary on the claude-enphase MCP server.

    Returns today's energy summary including 15-minute interval arrays for
    production, consumption, battery SOC, and grid import/export.

    Raises:
        Exception if the MCP server is unreachable or the tool call fails.
    """
    log.info("[enphase_mcp] Calling enphase_get_energy_summary")
    args: dict = {}
    if date_str:
        args["date"] = date_str
    return await _call_tool("enphase_get_energy_summary", args)


async def get_battery_mode() -> dict:
    """
    Call enphase_get_battery_settings on the claude-enphase MCP server.

    Returns the raw battery settings dict. The active profile is in the "usage"
    field (e.g. "self-consumption", "cost_savings").

    Raises:
        Exception if the MCP server is unreachable or the tool call fails.
    """
    log.info("[enphase_mcp] Calling enphase_get_battery_settings")
    return await _call_tool("enphase_get_battery_settings")


async def set_battery_mode(mode: str) -> dict:
    """
    Call enphase_set_battery_profile on the claude-enphase MCP server.

    Args:
        mode: Target Enphase battery profile (e.g. "self-consumption", "cost_savings").

    Returns the server's response dict.

    Raises:
        Exception if the MCP server is unreachable or the tool call fails.
    """
    log.info("[enphase_mcp] Calling enphase_set_battery_profile(profile=%s)", mode)
    return await _call_tool("enphase_set_battery_profile", {"profile": mode})


async def get_active_grid_event() -> bool:
    """
    Return True if an APS Storage Rewards dispatch event is currently active.
    Returns False on any error so a check failure never blocks a mode switch.

    NOTE: Uses a best-effort heuristic in the claude-enphase server — the Enphase
    grid-services event API is not publicly documented. Verify the raw response
    field during a live event and update claude-enphase/server.py if needed.
    """
    log.info("[enphase_mcp] Calling enphase_get_grid_event")
    return await _call_tool_flag("enphase_get_grid_event")


async def get_storm_guard_active() -> bool:
    """
    Return True if Storm Guard is currently alerting (Enphase is charging battery to 100%).
    Returns False on any error so a check failure never blocks a mode switch.
    """
    log.info("[enphase_mcp] Calling enphase_get_storm_guard")
    return await _call_tool_flag("enphase_get_storm_guard")


async def get_tariff() -> dict:
    """
    Call enphase_get_tariff on the claude-enphase MCP server.

    Returns:
        The full TOU rate structure as a dict.

    Raises:
        Exception if the MCP server is unreachable or the tool call fails.
    """
    log.info("[enphase_mcp] Connecting to %s", ENPHASE_MCP_URL)
    log.info("[enphase_mcp] Calling enphase_get_tariff")
    data = await _call_tool("enphase_get_tariff")
    log.debug("[enphase_mcp] Tariff top-level keys: %s",
              list(data.keys()) if isinstance(data, dict) else type(data).__name__)
    return data
