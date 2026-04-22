"""
Email MCP client.

Connects to the claude-email MCP SSE server and sends alert emails.
Used by the battery-mode scheduler to notify Charles when a scheduled
Enphase mode switch fails after a retry.
"""

import json
import logging
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

log = logging.getLogger(__name__)

EMAIL_MCP_URL  = os.getenv("EMAIL_MCP_URL")
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL")


async def send_email(subject: str, body: str, to: str | None = None) -> dict:
    """
    Call send_email on the claude-email MCP server.

    Args:
        subject: Email subject line.
        body:    Plain-text email body.
        to:      Recipient (defaults to ALERT_TO_EMAIL env var / charles).

    Returns the server's response dict. Raises on connection/tool failure.
    """
    recipient = to or ALERT_TO_EMAIL
    log.info("[email_mcp] Sending alert email to %s (subject=%r)", recipient, subject)
    async with sse_client(EMAIL_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "send_email",
                {"to": recipient, "subject": subject, "body": body},
            )
            if result.isError or not result.content:
                text = result.content[0].text if result.content else "empty response"
                raise RuntimeError(f"send_email failed: {text}")
            text = result.content[0].text
            if text.startswith("Error:"):
                raise RuntimeError(f"send_email failed: {text}")
            try:
                return json.loads(text)
            except (ValueError, AttributeError):
                return {"raw": text}
