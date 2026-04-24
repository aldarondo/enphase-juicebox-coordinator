"""
Email client for the coordinator.

Calls the brian-email REST endpoint directly (/api/send-email) rather than
going through the MCP SSE handshake. The Python MCP SSE client and the JS
SSE SDK have a protocol mismatch that causes 400 errors on the POST phase.
"""

import json
import logging
import os

import httpx

log = logging.getLogger(__name__)

EMAIL_MCP_URL    = os.getenv("EMAIL_MCP_URL", "")
EMAIL_MCP_API_KEY = os.getenv("EMAIL_MCP_API_KEY")
ALERT_TO_EMAIL   = os.getenv("ALERT_TO_EMAIL")

# Derive the base URL from EMAIL_MCP_URL (strip /sse suffix if present)
def _base_url() -> str:
    url = EMAIL_MCP_URL.rstrip("/")
    if url.endswith("/sse"):
        url = url[: -len("/sse")]
    return url


async def send_email(subject: str, body: str, to: str | None = None) -> dict:
    """
    POST to brian-email's /api/send-email REST endpoint.

    Args:
        subject: Email subject line.
        body:    Plain-text email body.
        to:      Recipient (defaults to ALERT_TO_EMAIL env var).

    Returns the server's response dict. Raises on connection/tool failure.
    """
    recipient = to or ALERT_TO_EMAIL
    log.info("[email_mcp] Sending alert email to %s (subject=%r)", recipient, subject)

    base = _base_url()
    if not base:
        raise RuntimeError("EMAIL_MCP_URL is not configured")

    url = f"{base}/api/send-email"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if EMAIL_MCP_API_KEY:
        headers["Authorization"] = f"Bearer {EMAIL_MCP_API_KEY}"

    payload = {"to": recipient, "subject": subject, "body": body}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"send_email failed: HTTP {resp.status_code} — {resp.text[:200]}"
            )
        try:
            return resp.json()
        except (ValueError, AttributeError):
            return {"raw": resp.text}
