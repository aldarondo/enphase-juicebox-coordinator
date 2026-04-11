"""
Coordinator: fetch Enphase tariff → find peak hours → program JuiceBox to avoid them.
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
    1. Fetch TOU tariff from Enphase Enlighten
    2. Find the peak pricing window
    3. Set JuiceBox to charge during all non-peak hours
    """
    started_at = datetime.now(ARIZONA).isoformat()
    log.info("[coordinator] Run started at %s", started_at)

    result: dict = {
        "started_at":        started_at,
        "status":            "ok",
        "errors":            [],
        "schedule":          [],
        "reasoning":         "",
        "juicebox_ok":       False,
        "juicebox_response": None,
    }

    # ── Fetch tariff ────────────────────────────────────────────────────────
    tariff = {}
    try:
        tariff = await enphase.get_client().get_tariff()
        log.info("[coordinator] Tariff fetched OK")
    except Exception as exc:
        msg = f"Failed to fetch tariff: {exc}"
        log.error("[coordinator] %s", msg)
        result["errors"].append(msg)
        # Proceed anyway — optimizer will use APS defaults

    # ── Compute schedule ────────────────────────────────────────────────────
    schedule, reasoning = optimizer.compute_schedule(tariff)
    result["schedule"]  = schedule
    result["reasoning"] = reasoning

    # ── Push to JuiceBox ────────────────────────────────────────────────────
    try:
        jb_resp = await juicebox_mcp.set_charging_schedule(schedule)
        result["juicebox_ok"]       = True
        result["juicebox_response"] = jb_resp
        log.info("[coordinator] JuiceBox schedule set OK")
    except Exception as exc:
        msg = f"Failed to set JuiceBox schedule: {exc}"
        log.error("[coordinator] %s", msg)
        result["errors"].append(msg)

    if result["errors"]:
        result["status"] = "error" if not result["juicebox_ok"] else "partial"

    result["finished_at"] = datetime.now(ARIZONA).isoformat()
    log.info("[coordinator] Done — status: %s", result["status"])
    return result
