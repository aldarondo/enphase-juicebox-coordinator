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

ENPHASE_MCP_URL = os.getenv("ENPHASE_MCP_URL", "http://192.168.0.64:8766/sse")


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
            if result.content:
                text = result.content[0].text
                try:
                    return json.loads(text)
                except (ValueError, AttributeError):
                    return {"raw": text}
            return {}
