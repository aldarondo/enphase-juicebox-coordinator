"""
Enphase battery mode scheduling.

Switches the Enphase battery profile at the peak-pricing window boundaries:

  15:57 Arizona  Savings → Self-Consumption
                 (solar covers load first; battery only fills the gap;
                  excess solar charges the battery instead of exporting at
                  a low rate during the 16:00–19:00 peak window)

  19:02 Arizona  Self-Consumption → Savings
                 (solar is gone; restore TOU-aware discharge for the evening)

Each switch reads the current mode, skips if already on target (manual
correction), pushes the new mode, confirms, and on failure retries once then
emails Charles via claude-email.
"""

import asyncio
import logging
from datetime import datetime

import pytz

import email_mcp
import enphase_mcp

log = logging.getLogger(__name__)
ARIZONA = pytz.timezone("America/Phoenix")

MODE_SELF_CONSUMPTION = "self-consumption"
MODE_SAVINGS          = "savings"

RETRY_DELAY_SECONDS = 10

# Human-readable consequences for failure alerts. Keyed on target mode.
FAILURE_CONSEQUENCE = {
    MODE_SELF_CONSUMPTION: (
        "Enphase stays in Savings Mode during the 16:00–19:00 peak window — "
        "battery will cycle unnecessarily while solar exports at the low rate."
    ),
    MODE_SAVINGS: (
        "Enphase stays in Self-Consumption through the evening — "
        "TOU optimization is lost for the night."
    ),
}


def _extract_mode(payload) -> str | None:
    """Pull a mode string out of the loosely-typed enphase_mcp response."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("mode", "battery_mode", "profile", "battery_profile"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return None


async def _send_failure_alert(label: str, target_mode: str, error: str) -> None:
    """Notify Charles that a mode switch failed after retry."""
    subject = f"[enphase-coordinator] {label} mode switch FAILED"
    body = (
        f"Scheduled mode switch failed: {label}\n"
        f"Target mode: {target_mode}\n"
        f"Error: {error}\n\n"
        f"Consequence: {FAILURE_CONSEQUENCE.get(target_mode, 'Unknown mode — manual check recommended.')}\n\n"
        f"Recovery: run `switch_battery_mode` in the coordinator to retry, "
        f"or change the mode manually in the Enphase app."
    )
    try:
        await email_mcp.send_email(subject=subject, body=body)
        log.info("[battery_mode] Failure alert email sent for %s", label)
    except Exception as exc:
        log.error("[battery_mode] Failed to send failure alert email: %s", exc)


async def switch_to(target_mode: str, label: str) -> dict:
    """
    Switch the Enphase battery profile to target_mode.

    Flow:
        1. Read current mode.
        2. If already target_mode, skip (manual correction may have occurred).
        3. Set target_mode.
        4. Confirm via API response.
        5. On any failure (get/set/confirm), retry once after RETRY_DELAY_SECONDS.
        6. If retry also fails, email Charles.

    Returns a structured result dict suitable for logging / MCP response.
    """
    started_at = datetime.now(ARIZONA).isoformat()
    log.info("[battery_mode] %s: switching to %s", label, target_mode)

    result: dict = {
        "label":        label,
        "target_mode":  target_mode,
        "started_at":   started_at,
        "status":       "unknown",
        "current_mode": None,
        "attempts":     0,
        "errors":       [],
    }

    last_error: str | None = None
    for attempt in (1, 2):
        result["attempts"] = attempt
        try:
            current_payload = await enphase_mcp.get_battery_mode()
            current_mode = _extract_mode(current_payload)
            result["current_mode"] = current_mode

            if current_mode == target_mode:
                result["status"]  = "skipped_already_target"
                result["message"] = (
                    f"Already in {target_mode} (manual correction may have occurred); skipping."
                )
                result["finished_at"] = datetime.now(ARIZONA).isoformat()
                log.info("[battery_mode] %s: %s", label, result["message"])
                return result

            set_payload = await enphase_mcp.set_battery_mode(target_mode)
            confirmed_mode = _extract_mode(set_payload) or target_mode
            result["applied_mode"] = confirmed_mode

            if confirmed_mode != target_mode:
                raise RuntimeError(
                    f"Enphase confirmed mode={confirmed_mode!r}, expected {target_mode!r}"
                )

            result["status"]      = "ok"
            result["message"]     = f"Switched {current_mode} → {target_mode}"
            result["finished_at"] = datetime.now(ARIZONA).isoformat()
            log.info("[battery_mode] %s: %s", label, result["message"])
            return result

        except Exception as exc:
            last_error = str(exc)
            result["errors"].append(f"attempt {attempt}: {last_error}")
            log.warning("[battery_mode] %s attempt %d failed: %s", label, attempt, last_error)
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    # Both attempts failed — alert and return error result
    result["status"]      = "error"
    result["finished_at"] = datetime.now(ARIZONA).isoformat()
    log.error("[battery_mode] %s: both attempts failed — sending alert", label)
    await _send_failure_alert(label, target_mode, last_error or "unknown error")
    return result


async def switch_to_self_consumption() -> dict:
    """15:57 Arizona job: Savings → Self-Consumption before the 16:00 peak."""
    return await switch_to(MODE_SELF_CONSUMPTION, label="15:57 pre-peak")


async def switch_to_savings() -> dict:
    """19:02 Arizona job: Self-Consumption → Savings after the 19:00 peak."""
    return await switch_to(MODE_SAVINGS, label="19:02 post-peak")
