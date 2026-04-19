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
