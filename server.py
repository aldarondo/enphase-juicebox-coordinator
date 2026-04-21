"""
Enphase–JuiceBox Coordinator MCP Server

Exposes tools so Claude can trigger and inspect the coordinator:

  run_coordinator   — fetch Enphase rates, compute optimal charging windows,
                      push schedule to JuiceBox MCP.
  get_last_run      — return the result from the most recent coordinator run.
  charge_now        — override TOU schedule and charge immediately.
  get_weekly_report — return the most recent Sunday weekly report.

Also runs background APScheduler jobs:
  - Daily 04:00 Arizona: coordinator run (keep JuiceBox schedule current)
  - Sunday 06:00 Arizona: weekly report (logs summary to container logs)

Transport modes (set MCP_TRANSPORT env var):
  stdio (default) — Claude Code subprocess; use for local dev
  sse             — persistent HTTP server on MCP_PORT (default 8767);
                    use for NAS/Docker deployment so the scheduler runs 24/7

Add to Claude Desktop (SSE, NAS deployment):
  "coordinator": { "type": "sse", "url": "http://<NAS>:8767/sse" }
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import calendar_check
import coordinator
import enphase_mcp
import juicebox_mcp
import optimizer
import surplus_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coordinator-mcp")
ARIZONA = pytz.timezone("America/Phoenix")

app = Server("enphase-juicebox-coordinator")

# ── Shared state ──────────────────────────────────────────────────────────────
_last_result:   dict | None = None
_last_report:   dict | None = None
_cached_tariff: dict        = {}   # updated each daily coordinator run
_overnight_charging: dict   = {
    "enabled":         True,
    "reason":          "default — calendar check has not run yet",
    "set_at":          None,
    "calendar_result": None,
}
_surplus_state: dict        = {
    "mode":               "tou_schedule",  # "tou_schedule" | "surplus_override"
    "surplus_poll_count":    0,
    "no_surplus_poll_count": 0,
    "battery_soc":        None,
    "production_w":       None,
    "consumption_w":      None,
    "surplus_w":          None,
    "charge_amps":        None,
    "last_checked":       None,
    "last_action":        None,
}

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="run_coordinator",
        description=(
            "Fetch the current Enphase TOU rate schedule, compute the optimal "
            "JuiceBox EV charging windows for the week (preferring cheapest rate "
            "hours), and push the schedule to the JuiceBox MCP server. "
            "Returns the full result including the computed schedule and reasoning."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_last_run",
        description=(
            "Return the result from the most recent coordinator run — "
            "the computed schedule, reasoning, and any errors. "
            "Useful for checking what schedule is currently programmed and why."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="charge_now",
        description=(
            "Override the TOU schedule and allow the JuiceBox to charge immediately. "
            "Pushes a charging window starting now for the specified number of hours "
            "(default: until end of today). The normal TOU schedule resumes at the "
            "next 04:00 coordinator run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "number",
                    "description": "Hours to charge from now (default: until 23:59 today).",
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="get_overnight_mode",
        description=(
            "Return the current overnight charging decision — whether the TOU "
            "schedule will run tonight, why, and the calendar check result that "
            "triggered it. The nightly calendar check runs at 21:00 Arizona and "
            "enables overnight TOU charging if tomorrow's driving exceeds the "
            "threshold (default 80 miles)."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="set_overnight_mode",
        description=(
            "Manually override tonight's charging mode. "
            "Pass enable=true to force TOU overnight charging (e.g. you know "
            "tomorrow will be a long drive), or enable=false to skip overnight "
            "charging (surplus solar only). Resets to default after the 04:00 run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enable": {
                    "type":        "boolean",
                    "description": "True = charge overnight on TOU schedule; False = surplus only.",
                },
                "reason": {
                    "type":        "string",
                    "description": "Why you're overriding (for logging).",
                },
            },
            "required": ["enable", "reason"],
        },
    ),
    Tool(
        name="get_surplus_status",
        description=(
            "Return the current state of the surplus solar monitor — "
            "battery SOC, solar production vs. consumption, whether surplus "
            "charging is active, charge amps, and when the monitor last ran. "
            "The monitor polls every 15 minutes during non-peak daylight hours "
            "and activates JuiceBox when the battery is full and solar exceeds "
            "home consumption."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_weekly_report",
        description=(
            "Return the most recent Sunday weekly report — current schedule, "
            "last coordinator run status, and tariff source. Generated automatically "
            "every Sunday at 06:00 Arizona time."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]

# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _last_result, _last_report

    if name == "run_coordinator":
        log.info("Tool: run_coordinator triggered by Claude")
        try:
            _last_result = await coordinator.run()
        except Exception as exc:
            log.exception("run_coordinator failed")
            _last_result = {"status": "error", "error": str(exc)}
        return [TextContent(type="text", text=json.dumps(_last_result, indent=2))]

    if name == "get_last_run":
        if _last_result is None:
            payload = {
                "status":  "never_run",
                "message": "The coordinator has not run yet in this session. "
                           "Call run_coordinator to trigger it, or wait for the "
                           "daily 04:00 scheduled run.",
            }
        else:
            payload = _last_result
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    if name == "charge_now":
        now = datetime.now(ARIZONA)
        day_name = now.strftime("%a").lower()  # "mon", "tue", etc.
        start_str = now.strftime("%H:%M")

        hours = arguments.get("hours")
        if hours:
            end_dt = now + timedelta(hours=float(hours))
            # Cap at end of day
            end_of_day = now.replace(hour=23, minute=59, second=0, microsecond=0)
            end_dt = min(end_dt, end_of_day)
            end_str = end_dt.strftime("%H:%M")
        else:
            end_str = "23:59"

        override_schedule = [{
            "label":    f"Manual override — charge now until {end_str}",
            "days":     [day_name],
            "start":    start_str,
            "end":      end_str,
            "max_amps": 32,
        }]

        log.info("Tool: charge_now — pushing override window %s–%s (%s)", start_str, end_str, day_name)
        try:
            jb_resp = await juicebox_mcp.set_charging_schedule(override_schedule)
            payload = {
                "status":            "ok",
                "override_active":   True,
                "window":            override_schedule[0],
                "juicebox_response": jb_resp,
                "note":              "Normal TOU schedule resumes at next 04:00 coordinator run.",
            }
        except Exception as exc:
            log.error("charge_now failed: %s", exc)
            payload = {"status": "error", "error": str(exc)}
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    if name == "get_overnight_mode":
        return [TextContent(type="text", text=json.dumps(_overnight_charging, indent=2))]

    if name == "set_overnight_mode":
        enabled = arguments.get("enable", True)
        reason  = arguments.get("reason", "manual override")
        _overnight_charging.update({
            "enabled":         enabled,
            "reason":          f"Manual override: {reason}",
            "set_at":          datetime.now(ARIZONA).isoformat(),
            "calendar_result": None,
        })
        log.info("Tool: set_overnight_mode → enabled=%s  reason=%s", enabled, reason)
        return [TextContent(type="text", text=json.dumps({
            "status":  "ok",
            "enabled": enabled,
            "reason":  reason,
            "note":    "Resets to default (enabled=True) after the 04:00 coordinator run.",
        }, indent=2))]

    if name == "get_surplus_status":
        peak = optimizer._find_peak_weekday_hours(_cached_tariff) or optimizer.APS_DEFAULT_PEAK
        payload = dict(_surplus_state)
        payload["peak_window"] = f"{peak['start_h']:02d}:00–{peak['end_h']:02d}:00 weekdays"
        payload["thresholds"] = {
            "battery_full_soc":  surplus_monitor.BATTERY_FULL_SOC,
            "battery_low_soc":   surplus_monitor.BATTERY_LOW_SOC,
            "surplus_min_w":     surplus_monitor.SURPLUS_MIN_W,
            "activation_polls":  surplus_monitor.ACTIVATION_POLLS,
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    if name == "get_weekly_report":
        if _last_report is None:
            payload = {
                "status":  "no_report",
                "message": "No weekly report has been generated yet. "
                           "Reports are generated automatically every Sunday at 06:00 Arizona time.",
            }
        else:
            payload = _last_report
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _scheduled_run():
    global _last_result, _cached_tariff, _overnight_charging
    enabled = _overnight_charging.get("enabled", True)
    reason  = _overnight_charging.get("reason", "")
    log.info("[scheduler] Daily coordinator run — overnight_charging=%s  (%s)", enabled, reason)

    if enabled:
        try:
            _last_result = await coordinator.run()
            log.info("[scheduler] Done — status: %s", _last_result.get("status"))
        except Exception:
            log.exception("[scheduler] Daily run failed")
    else:
        # No long trip tomorrow — skip overnight charging.
        # Set a daytime-only schedule (super off-peak window) instead of clearing
        # entirely, so the car still charges during the cheapest rate window.
        log.info("[scheduler] Overnight disabled — setting daytime-only schedule")
        try:
            tariff = _cached_tariff or {}
            schedule, sched_reasoning = optimizer.compute_schedule(
                tariff, overnight_enabled=False
            )
            jb_resp = await juicebox_mcp.set_charging_schedule(schedule)
            _last_result = {
                "started_at":        datetime.now(ARIZONA).isoformat(),
                "status":            "ok",
                "schedule":          schedule,
                "reasoning":         f"Overnight disabled ({reason}). {sched_reasoning}",
                "juicebox_ok":       True,
                "juicebox_response": jb_resp,
                "errors":            [],
                "finished_at":       datetime.now(ARIZONA).isoformat(),
            }
        except Exception as exc:
            log.error("[scheduler] Failed to set daytime schedule: %s", exc)

    # Reset overnight mode — safe default is always to charge
    _overnight_charging = {
        "enabled":         True,
        "reason":          "reset after 04:00 coordinator run",
        "set_at":          None,
        "calendar_result": None,
    }

    # Refresh cached tariff so surplus monitor has current peak hours
    try:
        _cached_tariff = await enphase_mcp.get_tariff()
    except Exception:
        log.warning("[scheduler] Could not refresh cached tariff after coordinator run")


async def _verify_schedule_against_tariff() -> dict:
    """
    Fetch live tariff, recompute expected schedule, and compare to what was
    last programmed. Returns a verification summary dict.
    """
    verification: dict = {"status": "unknown"}
    try:
        tariff = await enphase_mcp.get_tariff()
        expected_schedule, reasoning = optimizer.compute_schedule(tariff)
        verification["tariff_source"] = reasoning
        verification["expected_schedule"] = expected_schedule

        if _last_result and _last_result.get("schedule"):
            programmed = _last_result["schedule"]
            # Compare weekday windows by start/end times
            def _weekday_window(sched):
                return next((w for w in sched if "mon" in w.get("days", [])), None)

            exp_wd = _weekday_window(expected_schedule)
            prog_wd = _weekday_window(programmed)

            if exp_wd and prog_wd:
                if exp_wd["start"] == prog_wd["start"] and exp_wd["end"] == prog_wd["end"]:
                    verification["status"] = "in_sync"
                    verification["message"] = (
                        f"Programmed weekday window ({prog_wd['start']}–{prog_wd['end']}) "
                        f"matches current tariff."
                    )
                else:
                    verification["status"] = "drift_detected"
                    verification["message"] = (
                        f"MISMATCH: programmed {prog_wd['start']}–{prog_wd['end']} "
                        f"but tariff now indicates {exp_wd['start']}–{exp_wd['end']}. "
                        f"Run coordinator to resync."
                    )
            else:
                verification["status"] = "could_not_compare"
        else:
            verification["status"] = "no_programmed_schedule"
            verification["message"] = "No programmed schedule to compare against."
    except Exception as exc:
        verification["status"] = "error"
        verification["error"] = str(exc)
        log.warning("[weekly_report] Tariff verification failed: %s", exc)

    return verification


async def _scheduled_weekly_report():
    global _last_report
    now = datetime.now(ARIZONA)
    log.info("[scheduler] Weekly report triggered (Sunday 06:00 Arizona)")

    report: dict = {
        "generated_at": now.isoformat(),
        "week_ending":  now.strftime("%Y-%m-%d"),
    }

    if _last_result:
        report["last_coordinator_run"] = {
            "started_at":  _last_result.get("started_at"),
            "finished_at": _last_result.get("finished_at"),
            "status":      _last_result.get("status"),
            "juicebox_ok": _last_result.get("juicebox_ok"),
            "reasoning":   _last_result.get("reasoning"),
            "errors":      _last_result.get("errors", []),
        }
        report["current_schedule"] = _last_result.get("schedule", [])
    else:
        report["last_coordinator_run"] = None
        report["current_schedule"] = []
        report["note"] = "Coordinator has not run yet this session."

    report["schedule_verification"] = await _verify_schedule_against_tariff()
    _last_report = report

    # Log the report prominently so it appears in container logs
    log.info("=" * 60)
    log.info("WEEKLY REPORT — %s", now.strftime("%Y-%m-%d"))
    log.info("=" * 60)
    if _last_result:
        run = report["last_coordinator_run"]
        log.info("  Last run:    %s  status=%s  juicebox_ok=%s",
                 run["started_at"], run["status"], run["juicebox_ok"])
        log.info("  Reasoning:   %s", run["reasoning"])
        if run["errors"]:
            log.info("  Errors:      %s", "; ".join(run["errors"]))
        for window in report["current_schedule"]:
            log.info("  Schedule:    %s  %s–%s  [%s]",
                     ",".join(window.get("days", [])),
                     window.get("start"), window.get("end"),
                     window.get("label", ""))
    else:
        log.info("  No coordinator run recorded this session.")
    v = report["schedule_verification"]
    log.info("  Verification: [%s] %s", v["status"], v.get("message", v.get("error", "")))
    log.info("=" * 60)


async def _nightly_calendar_check() -> None:
    """
    Runs at 21:00 Arizona. Fetches tomorrow's calendar events, estimates total
    driving distance, and sets _overnight_charging accordingly.

    If tomorrow's driving >= DRIVING_THRESHOLD_MILES, overnight TOU charging
    is enabled so the car won't wake up empty waiting for the sun.
    If no iCal URLs are configured, overnight charging stays enabled (safe default).
    """
    global _overnight_charging
    ical_urls = [u.strip() for u in os.getenv("GOOGLE_ICAL_URLS", "").split(",") if u.strip()]

    if not ical_urls:
        log.info("[calendar_check] GOOGLE_ICAL_URLS not configured — overnight charging stays enabled")
        return

    log.info("[calendar_check] Running nightly calendar check (%d feed(s))", len(ical_urls))
    try:
        result = await calendar_check.check_tomorrow_driving(ical_urls)
        _overnight_charging = {
            "enabled":         result["overnight_charging_needed"],
            "reason":          result["reasoning"],
            "set_at":          datetime.now(ARIZONA).isoformat(),
            "calendar_result": result,
        }
        log.info("[calendar_check] %s", result["reasoning"])
    except Exception as exc:
        log.error("[calendar_check] Check failed — leaving overnight charging enabled: %s", exc)


async def _activate_surplus_charging(amps: int, now: datetime) -> None:
    """Push a surplus-charging window to JuiceBox and update shared state."""
    global _surplus_state
    day_name  = now.strftime("%a").lower()
    start_str = now.strftime("%H:%M")
    surplus_w = _surplus_state.get("surplus_w") or 0
    override  = [{
        "label":    f"Surplus solar — {amps}A (~{surplus_w}W excess)",
        "days":     [day_name],
        "start":    start_str,
        "end":      "23:59",
        "max_amps": amps,
    }]
    try:
        await juicebox_mcp.set_charging_schedule(override)
        _surplus_state["mode"]        = "surplus_override"
        _surplus_state["charge_amps"] = amps
        _surplus_state["last_action"] = f"activated {amps}A surplus charging at {now.isoformat()}"
        log.info("[surplus_monitor] ACTIVATED surplus charging at %dA (~%dW excess)", amps, surplus_w)
    except Exception as exc:
        log.error("[surplus_monitor] Failed to activate surplus charging: %s", exc)


async def _revert_to_tou_schedule() -> None:
    """Restore the TOU schedule and reset surplus state."""
    global _surplus_state
    if _last_result and _last_result.get("schedule"):
        schedule = _last_result["schedule"]
    else:
        schedule, _ = optimizer.compute_schedule(_cached_tariff)
    try:
        await juicebox_mcp.set_charging_schedule(schedule)
        _surplus_state["mode"]               = "tou_schedule"
        _surplus_state["surplus_poll_count"]    = 0
        _surplus_state["no_surplus_poll_count"] = 0
        _surplus_state["charge_amps"]        = None
        _surplus_state["last_action"] = f"reverted to TOU schedule ({datetime.now(ARIZONA).isoformat()})"
        log.info("[surplus_monitor] REVERTED to TOU schedule")
    except Exception as exc:
        log.error("[surplus_monitor] Failed to revert to TOU schedule: %s", exc)


async def _surplus_monitor_run() -> None:
    """
    Polls Enphase every 15 minutes during non-peak daylight hours.
    Activates JuiceBox when battery is full and solar exceeds consumption.
    Reverts to TOU schedule when surplus ends.
    Skips the peak pricing window entirely — car never charges during peak.
    """
    global _surplus_state
    now = datetime.now(ARIZONA)

    # Only run during daylight hours (06:00–20:00 Arizona)
    if now.hour < 6 or now.hour >= 20:
        return

    # Determine peak window from cached tariff (fallback to APS default)
    peak = optimizer._find_peak_weekday_hours(_cached_tariff) or optimizer.APS_DEFAULT_PEAK
    if surplus_monitor.is_peak_time(now.hour, now.minute, peak["start_h"], peak["end_h"]):
        log.debug("[surplus_monitor] Skipping — peak window (%02d:00–%02d:00)",
                  peak["start_h"], peak["end_h"])
        # Safety: if we somehow entered surplus mode and peak just started, revert
        if _surplus_state["mode"] == "surplus_override":
            log.warning("[surplus_monitor] Peak started while surplus override active — reverting")
            await _revert_to_tou_schedule()
        return

    # Fetch current energy state
    try:
        summary = await enphase_mcp.get_energy_summary()
    except Exception as exc:
        log.warning("[surplus_monitor] Could not fetch energy summary: %s", exc)
        return

    values   = surplus_monitor.extract_current_values(summary)
    soc      = values["battery_soc"]
    prod     = values["production_w"]
    cons     = values["consumption_w"]
    surplus_w = max(0, prod - cons)

    _surplus_state.update({
        "battery_soc":   soc,
        "production_w":  prod,
        "consumption_w": cons,
        "surplus_w":     surplus_w,
        "last_checked":  now.isoformat(),
    })

    if surplus_monitor.is_surplus(values):
        _surplus_state["surplus_poll_count"]    += 1
        _surplus_state["no_surplus_poll_count"]  = 0

        if _surplus_state["surplus_poll_count"] >= surplus_monitor.ACTIVATION_POLLS:
            amps = surplus_monitor.compute_charge_amps(surplus_w)
            # Activate on first trigger, or update amps if production changed
            if (_surplus_state["mode"] == "tou_schedule" or
                    amps != _surplus_state.get("charge_amps")):
                await _activate_surplus_charging(amps, now)

    elif surplus_monitor.is_no_longer_surplus(values):
        _surplus_state["no_surplus_poll_count"] += 1
        _surplus_state["surplus_poll_count"]     = 0

        if (_surplus_state["mode"] == "surplus_override" and
                _surplus_state["no_surplus_poll_count"] >= surplus_monitor.DEACTIVATION_POLLS):
            await _revert_to_tou_schedule()

    log.info(
        "[surplus_monitor] SOC=%s%%  prod=%dW  cons=%dW  surplus=%dW  mode=%s",
        soc, prod, cons, surplus_w, _surplus_state["mode"],
    )


def _build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ARIZONA)
    scheduler.add_job(
        _scheduled_run,
        "cron",
        hour=4,
        minute=0,
        id="daily_coordinator",
    )
    scheduler.add_job(
        _scheduled_weekly_report,
        "cron",
        day_of_week="sun",
        hour=6,
        minute=0,
        id="weekly_report",
    )
    scheduler.add_job(
        _surplus_monitor_run,
        "interval",
        minutes=15,
        id="surplus_monitor",
    )
    scheduler.add_job(
        _nightly_calendar_check,
        "cron",
        hour=21,
        minute=0,
        id="calendar_check",
    )
    return scheduler

# ── Entry point ───────────────────────────────────────────────────────────────

async def _run_stdio():
    scheduler = _build_scheduler()
    scheduler.start()
    next_daily    = scheduler.get_job("daily_coordinator").next_run_time
    next_report   = scheduler.get_job("weekly_report").next_run_time
    next_surplus  = scheduler.get_job("surplus_monitor").next_run_time
    next_calendar = scheduler.get_job("calendar_check").next_run_time
    log.info("Coordinator MCP server starting (stdio)")
    log.info("  Daily scheduler:  next run at %s (America/Phoenix)", next_daily)
    log.info("  Weekly report:    next run at %s (America/Phoenix)", next_report)
    log.info("  Surplus monitor:  next run at %s (America/Phoenix)", next_surplus)
    log.info("  Calendar check:   next run at %s (America/Phoenix)", next_calendar)
    log.info("  JuiceBox MCP:     %s", os.getenv("JUICEBOX_MCP_URL", "http://<YOUR-NAS-IP>:3001/sse"))

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

    scheduler.shutdown()


def _run_sse(host: str, port: int):
    from contextlib import asynccontextmanager
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    import uvicorn

    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())

    async def handle_report(request: Request):
        """GET /report — return the latest weekly report as JSON."""
        return JSONResponse(_last_report or {"status": "no_report",
                                             "message": "No report generated yet."})

    @asynccontextmanager
    async def lifespan(starlette_app):
        scheduler = _build_scheduler()
        scheduler.start()
        next_daily    = scheduler.get_job("daily_coordinator").next_run_time
        next_report   = scheduler.get_job("weekly_report").next_run_time
        next_surplus  = scheduler.get_job("surplus_monitor").next_run_time
        next_calendar = scheduler.get_job("calendar_check").next_run_time
        log.info("Coordinator MCP server starting (SSE) on %s:%d", host, port)
        log.info("  Daily scheduler:  next run at %s (America/Phoenix)", next_daily)
        log.info("  Weekly report:    next run at %s (America/Phoenix)", next_report)
        log.info("  Surplus monitor:  next run at %s (America/Phoenix)", next_surplus)
        log.info("  Calendar check:   next run at %s (America/Phoenix)", next_calendar)
        log.info("  JuiceBox MCP:     %s", os.getenv("JUICEBOX_MCP_URL", "http://<YOUR-NAS-IP>:3001/sse"))
        yield
        scheduler.shutdown()

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/report", endpoint=handle_report),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
        lifespan=lifespan,
    )
    uvicorn.run(starlette_app, host=host, port=port)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8767"))
        _run_sse(host, port)
    else:
        asyncio.run(_run_stdio())
