"""
Enphase MCP client.

Connects to the claude-enphase MCP SSE server and calls enphase_get_tariff.
Mirrors juicebox_mcp.py — the coordinator talks to both upstream MCPs the same way.
"""

import json
import logging
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

log = logging.getLogger(__name__)

ENPHASE_MCP_URL = os.getenv("ENPHASE_MCP_URL", "http://<YOUR-NAS-IP>:8766/sse")


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
    async with sse_client(ENPHASE_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("enphase_get_energy_summary", args)
            if result.isError or not result.content:
                text = result.content[0].text if result.content else "empty response"
                raise RuntimeError(f"enphase_get_energy_summary failed: {text}")
            text = result.content[0].text
            if text.startswith("Error:"):
                raise RuntimeError(f"enphase_get_energy_summary failed: {text}")
            try:
                return json.loads(text)
            except (ValueError, AttributeError) as exc:
                raise RuntimeError(
                    f"enphase_get_energy_summary returned non-JSON: {text[:200]}"
                ) from exc


async def get_battery_mode() -> dict:
    """
    Call enphase_get_battery_settings on the claude-enphase MCP server.

    Returns the raw battery settings dict. The active profile is in the "usage"
    field (e.g. "self-consumption", "cost_savings").

    Raises:
        Exception if the MCP server is unreachable or the tool call fails.
    """
    log.info("[enphase_mcp] Calling enphase_get_battery_settings")
    async with sse_client(ENPHASE_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("enphase_get_battery_settings", {})
            if result.isError or not result.content:
                text = result.content[0].text if result.content else "empty response"
                raise RuntimeError(f"enphase_get_battery_settings failed: {text}")
            text = result.content[0].text
            if text.startswith("Error:"):
                raise RuntimeError(f"enphase_get_battery_settings failed: {text}")
            try:
                return json.loads(text)
            except (ValueError, AttributeError) as exc:
                raise RuntimeError(
                    f"enphase_get_battery_settings returned non-JSON: {text[:200]}"
                ) from exc


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
    async with sse_client(ENPHASE_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("enphase_set_battery_profile", {"profile": mode})
            if result.isError or not result.content:
                text = result.content[0].text if result.content else "empty response"
                raise RuntimeError(f"enphase_set_battery_profile failed: {text}")
            text = result.content[0].text
            if text.startswith("Error:"):
                raise RuntimeError(f"enphase_set_battery_profile failed: {text}")
            try:
                return json.loads(text)
            except (ValueError, AttributeError) as exc:
                raise RuntimeError(
                    f"enphase_set_battery_profile returned non-JSON: {text[:200]}"
                ) from exc


async def get_storm_guard_active() -> bool:
    """
    Return True if Storm Guard is currently alerting (Enphase is charging battery to 100%).
    Returns False on any error so a check failure never blocks a mode switch.
    """
    log.info("[enphase_mcp] Calling enphase_get_storm_guard")
    try:
        async with sse_client(ENPHASE_MCP_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("enphase_get_storm_guard", {})
                if result.isError or not result.content:
                    return False
                text = result.content[0].text
                if text.startswith("Error:"):
                    return False
                data = json.loads(text)
                return bool(data.get("active", False))
    except Exception as exc:
        log.warning("[enphase_mcp] get_storm_guard_active failed (failing open): %s", exc)
        return False


async def get_tariff() -> dict:
    """
    Call enphase_get_tariff on the claude-enphase MCP server.

    Returns:
        The full TOU rate structure as a dict.

    Raises:
        Exception if the MCP server is unreachable or the tool call fails.
    """
    log.info("[enphase_mcp] Connecting to %s", ENPHASE_MCP_URL)
    async with sse_client(ENPHASE_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("[enphase_mcp] Calling enphase_get_tariff")
            result = await session.call_tool("enphase_get_tariff", {})
            if result.isError or not result.content:
                text = result.content[0].text if result.content else "empty response"
                raise RuntimeError(f"enphase_get_tariff failed: {text}")
            text = result.content[0].text
            # claude-enphase server returns plain "Error: ..." text on API failures
            if text.startswith("Error:"):
                raise RuntimeError(f"enphase_get_tariff failed: {text}")
            try:
                data = json.loads(text)
                log.debug("[enphase_mcp] Tariff top-level keys: %s",
                          list(data.keys()) if isinstance(data, dict) else type(data).__name__)
                return data
            except (ValueError, AttributeError) as exc:
                raise RuntimeError(f"enphase_get_tariff returned non-JSON: {text[:200]}") from exc
