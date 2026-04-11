"""
Coordinator: fetch Enphase data → compute schedule → program JuiceBox.

This is the single function the MCP server tool and the daily scheduler
both call. It returns a structured result dict that both can surface to
the user (via Claude or logs).
"""

import logging
from datetime import datetime
import pytz

import enphase
import optimizer
import juicebox_mcp

log = logging.getLogger(__name__)
ARIZONA = pytz.timezone("America/Phoenix")


async def run() -> dict:
    """
    End-to-end coordination run:
      1. Fetch TOU tariff from Enphase Enlighten
      2. Fetch current battery SOC from Enphase Enlighten
      3. Compute optimal JuiceBox charging schedule
      4. Push the schedule to the JuiceBox MCP server
      5. Return a full result dict for logging / Claude display

    Errors in individual steps are caught and surfaced in the result
    rather than crashing the whole run, so the scheduler stays alive.
    """
    started_at = datetime.now(ARIZONA).isoformat()
    log.info("[coordinator] Run started at %s", started_at)

    result: dict = {
        "started_at":  started_at,
        "status":      "ok",
        "errors":      [],
        "tariff_ok":   False,
        "status_ok":   False,
        "schedule":    [],
        "reasoning":   "",
        "juicebox_ok": False,
        "juicebox_response": None,
    }

    # ── Step 1: Fetch tariff ────────────────────────────────────────────────
    tariff = {}
    try:
        client = enphase.get_client()
        tariff = await client.get_tariff()
        result["tariff_ok"] = True
        log.info("[coordinator] Tariff fetched OK")
    except Exception as exc:
        msg = f"Failed to fetch tariff: {exc}"
        log.error("[coordinator] %s", msg)
        result["errors"].append(msg)

    # ── Step 2: Fetch battery SOC ───────────────────────────────────────────
    battery_soc = 50.0   # safe default if fetch fails
    try:
        status = await enphase.get_client().get_status()
        battery_soc = float(status.get("battery_soc_pct") or 50.0)
        result["battery_soc_pct"] = battery_soc
        result["status_ok"] = True
        log.info("[coordinator] Battery SOC: %.0f%%", battery_soc)
    except Exception as exc:
        msg = f"Failed to fetch battery status: {exc}"
        log.error("[coordinator] %s", msg)
        result["errors"].append(msg)
        result["battery_soc_pct"] = battery_soc   # report the fallback value used

    # ── Step 3: Compute schedule ────────────────────────────────────────────
    schedule, reasoning = optimizer.compute_schedule(tariff, battery_soc)
    result["schedule"]  = schedule
    result["reasoning"] = reasoning
    log.info("[coordinator] Reasoning: %s", reasoning)

    # ── Step 4: Push schedule to JuiceBox MCP ──────────────────────────────
    try:
        jb_resp = await juicebox_mcp.set_charging_schedule(schedule)
        result["juicebox_ok"]       = True
        result["juicebox_response"] = jb_resp
        log.info("[coordinator] JuiceBox schedule set OK: %s", jb_resp)
    except Exception as exc:
        msg = f"Failed to set JuiceBox schedule: {exc}"
        log.error("[coordinator] %s", msg)
        result["errors"].append(msg)

    # ── Finalise ────────────────────────────────────────────────────────────
    if result["errors"]:
        result["status"] = "partial" if (result["tariff_ok"] or result["status_ok"]) else "error"

    result["finished_at"] = datetime.now(ARIZONA).isoformat()
    log.info("[coordinator] Run finished — status: %s, errors: %d",
             result["status"], len(result["errors"]))
    return result
