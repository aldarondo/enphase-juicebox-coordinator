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
import os
from datetime import datetime

import pytz

import email_mcp
import enphase_mcp

# Set to true in .env once APS Storage Rewards enrollment is confirmed.
_STORAGE_REWARDS_ENROLLED = os.environ.get("STORAGE_REWARDS_ENROLLED", "false").lower() == "true"

log = logging.getLogger(__name__)
ARIZONA = pytz.timezone("America/Phoenix")

MODE_SELF_CONSUMPTION = "self-consumption"
MODE_SAVINGS          = "cost_savings"

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


def _is_storage_rewards_season() -> bool:
    """True during May–Oct (months 5–10), when APS Storage Rewards events can occur."""
    return datetime.now(ARIZONA).month in range(5, 11)


def _extract_mode(payload) -> str | None:
    """Pull a mode string out of the loosely-typed enphase_mcp response.

    Enphase API shapes seen in the wild:
      GET /batterySettings/ → {"type": "battery-details", "data": {"profile": "..."}}
      PUT /profile/         → {"profile": "...", ...} (flat)
      set_battery_profile wrapper → {"profile_set": "..."}
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        # New Enphase API: profile nested under "data"
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("profile", "usage", "mode", "battery_mode"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
        # Flat formats (old API, set wrapper, or direct strings)
        for key in ("usage", "profile_set", "mode", "battery_mode", "profile", "battery_profile"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return None


async def _send_failure_alert(label: str, target_mode: str, error: str) -> None:
    """Notify Charles that a mode switch failed after retry."""
    subject = f"ALERT: [enphase-coordinator] {label} mode switch FAILED (retries exhausted)"
    body = (
        f"Scheduled mode switch failed after all retry attempts: {label}\n"
        f"Target mode: {target_mode}\n"
        f"Error: {error}\n\n"
        f"ALL RETRIES EXHAUSTED — manual intervention required.\n\n"
        f"Consequence: {FAILURE_CONSEQUENCE.get(target_mode, 'Unknown mode — manual check recommended.')}\n\n"
        f"This condition will persist until the next scheduled switch or manual correction.\n\n"
        f"Recovery: run `switch_battery_mode` in the coordinator to retry, "
        f"or change the mode manually in the Enphase app."
    )
    try:
        await email_mcp.send_email(subject=subject, body=body)
        log.info("[battery_mode] Failure alert email sent for %s", label)
    except Exception as exc:
        log.error("[battery_mode] Failed to send failure alert email: %s", exc)


async def _send_status_email(result: dict) -> None:
    """Send a status email after a scheduled mode switch (success or failure)."""
    label       = result.get("label", "unknown")
    status      = result.get("status", "unknown")
    target_mode = result.get("target_mode", "unknown")
    applied     = result.get("applied_mode") or result.get("current_mode") or "—"
    errors      = result.get("errors", [])

    if status == "ok":
        subject = f"[enphase-coordinator] {label}: switched to {applied} ✓"
        body = (
            f"Battery mode switch completed successfully.\n\n"
            f"Label:    {label}\n"
            f"Target:   {target_mode}\n"
            f"Applied:  {applied}\n"
            f"Attempts: {result.get('attempts', '?')}\n"
        )
    elif status == "skipped_already_target":
        subject = f"[enphase-coordinator] {label}: already {applied}, skipped"
        body = (
            f"Battery mode switch skipped — already in target mode.\n\n"
            f"Label:    {label}\n"
            f"Mode:     {applied}\n"
        )
    else:
        subject = f"ALERT: [enphase-coordinator] {label}: FAILED after {result.get('attempts','?')} attempt(s)"
        error_lines = "\n".join(f"  - {e}" for e in errors) or "  (no detail)"
        body = (
            f"Battery mode switch FAILED.\n\n"
            f"Label:    {label}\n"
            f"Target:   {target_mode}\n"
            f"Attempts: {result.get('attempts', '?')}\n"
            f"Errors:\n{error_lines}\n\n"
            f"Consequence: {FAILURE_CONSEQUENCE.get(target_mode, 'Unknown mode — manual check recommended.')}\n\n"
            f"Recovery: run `switch_battery_mode` in the coordinator or change manually in the Enphase app."
        )

    try:
        await email_mcp.send_email(subject=subject, body=body)
        log.info("[battery_mode] Status email sent for %s (%s)", label, status)
    except Exception as exc:
        log.error("[battery_mode] Failed to send status email for %s: %s", label, exc)


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

    if await enphase_mcp.get_storm_guard_active():
        result["status"]  = "skipped_storm_guard"
        result["message"] = "Storm Guard alert is active — skipping mode switch to avoid disrupting storm prep charging."
        result["finished_at"] = datetime.now(ARIZONA).isoformat()
        log.info("[battery_mode] %s: %s", label, result["message"])
        return result

    if _STORAGE_REWARDS_ENROLLED and _is_storage_rewards_season():
        if await enphase_mcp.get_active_grid_event():
            result["status"]  = "skipped_aps_event"
            result["message"] = "APS Storage Rewards dispatch event active — skipping mode switch so APS controls the battery."
            result["finished_at"] = datetime.now(ARIZONA).isoformat()
            log.info("[battery_mode] %s: %s", label, result["message"])
            return result

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
            confirmed_mode = _extract_mode(set_payload)

            # If the set response doesn't echo back a mode (e.g. a response
            # shape like {"status": "ok", "queued": true} from a newer Enphase
            # API), fall back to an independent read to verify — never assume
            # success from an ambiguous payload.
            if confirmed_mode is None:
                log.info(
                    "[battery_mode] %s: set response had no mode field; verifying with a read",
                    label,
                )
                verify_payload = await enphase_mcp.get_battery_mode()
                confirmed_mode = _extract_mode(verify_payload)

            result["applied_mode"] = confirmed_mode

            if confirmed_mode != target_mode:
                raise RuntimeError(
                    f"Enphase did not confirm target mode (got {confirmed_mode!r}, expected {target_mode!r})"
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
    result = await switch_to(MODE_SELF_CONSUMPTION, label="15:57 pre-peak")
    # Always email on success/skip; failures already send a detailed alert via switch_to.
    if result.get("status") != "error":
        await _send_status_email(result)
    return result


async def switch_to_savings() -> dict:
    """19:02 Arizona job: Self-Consumption → Savings after the 19:00 peak."""
    return await switch_to(MODE_SAVINGS, label="19:02 post-peak")
