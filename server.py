"""
Enphase–JuiceBox Coordinator MCP Server

Exposes tools so Claude can trigger and inspect the coordinator:

  run_coordinator   — fetch Enphase rates, compute optimal charging windows,
                      push schedule to JuiceBox MCP.
  get_last_run      — return the result from the most recent coordinator run.
  charge_now        — override TOU schedule and charge immediately.
  get_weekly_report  — return the most recent Sunday weekly report.
  run_calendar_check — trigger the 21:00 calendar check on demand.

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

import battery_mode
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
_last_mode_switch: dict | None = None   # most recent battery-mode switch result
_scheduler:     "AsyncIOScheduler | None" = None  # exposed so tariff refresh can reschedule mode-switch jobs

# Minutes before peak_start (pre-peak switch) and after peak_end (post-peak
# switch). Tight buffers — large buffers waste off-peak / post-peak time.
PRE_PEAK_BUFFER_MIN  = 3
POST_PEAK_BUFFER_MIN = 2
_overnight_charging: dict   = {
    "enabled":         False,
    "reason":          "default — surplus solar only until 21:00 calendar check",
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
            "charging (surplus solar only). The schedule is pushed to the "
            "JuiceBox immediately so charging can start/stop right away. "
            "Flag resets to disabled (surplus-only) after the next 04:00 run."
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
    Tool(
        name="run_calendar_check",
        description=(
            "Trigger the calendar check now (normally runs at 21:00 Arizona). "
            "Reads Google Calendar iCal feeds, finds tomorrow's events, geocodes "
            "locations to estimate driving distance, and enables overnight TOU "
            "charging if a long trip (>50 miles) is detected. Immediately "
            "pushes the resulting schedule to JuiceBox — TOU schedule if enabled, "
            "or empty (surplus-only) if not — so charging can start right away."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="switch_battery_mode",
        description=(
            "Manually switch the Enphase battery profile. Normally handled by "
            "the scheduler: Savings → Self-Consumption at 15:57 Arizona (before "
            "the 16:00 peak), Self-Consumption → Savings at 19:02 (after 19:00 "
            "peak). Use this tool for on-demand switches, retries after a "
            "scheduled failure, or manual testing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {
                    "type":        "string",
                    "enum":        ["self-consumption", "savings"],
                    "description": "Target Enphase battery profile.",
                },
            },
            "required": ["mode"],
        },
    ),
    Tool(
        name="get_battery_mode_status",
        description=(
            "Return the result of the most recent battery-mode switch (scheduled "
            "or manual): target mode, applied mode, status (ok/skipped/error), "
            "attempts, and any errors. Useful for verifying the 15:57 / 19:02 "
            "scheduled switches ran cleanly."
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

        push_result = await _apply_overnight_decision(
            enabled,
            reasoning=f"Manual override: {reason}",
        )

        return [TextContent(type="text", text=json.dumps({
            "status":       "ok" if push_result else "push_failed",
            "enabled":      enabled,
            "reason":       reason,
            "juicebox_ok":  bool(push_result and push_result.get("juicebox_ok")),
            "note":         "Flag resets to disabled (surplus-only) after the next 04:00 coordinator run.",
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

    if name == "run_calendar_check":
        log.info("Tool: run_calendar_check triggered manually")
        await _nightly_calendar_check()
        return [TextContent(type="text", text=json.dumps(_overnight_charging, indent=2))]

    if name == "switch_battery_mode":
        global _last_mode_switch
        mode = arguments.get("mode")
        if mode not in (battery_mode.MODE_SELF_CONSUMPTION, battery_mode.MODE_SAVINGS):
            return [TextContent(type="text", text=json.dumps(
                {"status": "error", "error": f"invalid mode: {mode!r}"}
            ))]
        log.info("Tool: switch_battery_mode(mode=%s) triggered manually", mode)
        _last_mode_switch = await battery_mode.switch_to(mode, label="manual")
        return [TextContent(type="text", text=json.dumps(_last_mode_switch, indent=2))]

    if name == "get_battery_mode_status":
        if _last_mode_switch is None:
            payload = {
                "status":  "never_run",
                "message": "No battery-mode switch has occurred this session. "
                           "Scheduled switches run at 15:57 and 19:02 Arizona.",
            }
        else:
            payload = _last_mode_switch
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
        # No long trip tomorrow — surplus monitor is the primary charging mode.
        # Clear the JuiceBox schedule entirely so the car only charges when the
        # surplus monitor activates it (battery full + solar exceeds consumption).
        log.info("[scheduler] Overnight disabled — clearing schedule (surplus monitor primary)")
        try:
            jb_resp = await juicebox_mcp.set_charging_schedule([])
            _last_result = {
                "started_at":        datetime.now(ARIZONA).isoformat(),
                "status":            "ok",
                "schedule":          [],
                "reasoning":         f"Overnight TOU disabled ({reason}) — surplus solar is primary charging mode.",
                "juicebox_ok":       True,
                "juicebox_response": jb_resp,
                "errors":            [],
                "finished_at":       datetime.now(ARIZONA).isoformat(),
            }
        except Exception as exc:
            log.error("[scheduler] Failed to clear JuiceBox schedule: %s", exc)

    # Reset overnight mode — default is surplus-only until tonight's calendar check
    _overnight_charging = {
        "enabled":         False,
        "reason":          "reset after 04:00 coordinator run — surplus solar only",
        "set_at":          None,
        "calendar_result": None,
    }

    # Refresh cached tariff so surplus monitor + mode-switch jobs have current peak hours
    try:
        _cached_tariff = await enphase_mcp.get_tariff()
        _reschedule_battery_mode_jobs()
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


def _peak_switch_times(tariff: dict) -> dict:
    """
    Derive pre-peak and post-peak mode-switch times from the tariff's peak window.

    Returns {
        "pre_h": int, "pre_m": int,      # PRE_PEAK_BUFFER_MIN before peak_start
        "post_h": int, "post_m": int,    # POST_PEAK_BUFFER_MIN after peak_end
        "peak": {...},                   # the peak dict from optimizer
        "source": "tariff" | "default",
    }

    Falls back to APS default (16:00–19:00) if the tariff can't be parsed —
    APS peak has been stable for years, so this is a safe fallback.
    """
    peak   = optimizer._find_peak_weekday_hours(tariff)
    source = "tariff" if peak else "default"
    if not peak:
        peak = optimizer.APS_DEFAULT_PEAK

    pre_h = peak["start_h"] - 1
    pre_m = 60 - PRE_PEAK_BUFFER_MIN
    post_h = peak["end_h"]
    post_m = POST_PEAK_BUFFER_MIN
    return {"pre_h": pre_h, "pre_m": pre_m,
            "post_h": post_h, "post_m": post_m,
            "peak": peak, "source": source}


def _reschedule_battery_mode_jobs() -> None:
    """Reschedule the pre/post-peak jobs using the current cached tariff."""
    if _scheduler is None:
        return
    times = _peak_switch_times(_cached_tariff)
    try:
        _scheduler.reschedule_job(
            "battery_mode_pre_peak",
            trigger="cron",
            day_of_week="mon-fri",
            hour=times["pre_h"],
            minute=times["pre_m"],
            timezone=ARIZONA,
        )
        _scheduler.reschedule_job(
            "battery_mode_post_peak",
            trigger="cron",
            day_of_week="mon-fri",
            hour=times["post_h"],
            minute=times["post_m"],
            timezone=ARIZONA,
        )
        peak = times["peak"]
        log.info(
            "[scheduler] Battery-mode jobs rescheduled from %s: pre=%02d:%02d  post=%02d:%02d  (peak %02d:00–%02d:00 weekdays)",
            times["source"], times["pre_h"], times["pre_m"],
            times["post_h"], times["post_m"],
            peak["start_h"], peak["end_h"],
        )
    except Exception as exc:
        log.warning("[scheduler] Could not reschedule battery-mode jobs: %s", exc)


async def _scheduled_pre_peak_mode_switch() -> None:
    """Weekday pre-peak: Savings → Self-Consumption."""
    global _last_mode_switch
    peak = optimizer._find_peak_weekday_hours(_cached_tariff)
    if peak is None:
        log.info("[scheduler] Pre-peak mode switch skipped — tariff has no weekday peak window")
        _last_mode_switch = {
            "status":  "skipped_no_peak",
            "message": "Cached tariff has no weekday peak window; no switch needed today.",
        }
        return
    log.info("[scheduler] Pre-peak battery mode switch (peak starts %02d:00)", peak["start_h"])
    _last_mode_switch = await battery_mode.switch_to_self_consumption()


async def _scheduled_post_peak_mode_switch() -> None:
    """Weekday post-peak: Self-Consumption → Savings."""
    global _last_mode_switch
    peak = optimizer._find_peak_weekday_hours(_cached_tariff)
    if peak is None:
        log.info("[scheduler] Post-peak mode switch skipped — tariff has no weekday peak window")
        _last_mode_switch = {
            "status":  "skipped_no_peak",
            "message": "Cached tariff has no weekday peak window; no switch needed today.",
        }
        return
    log.info("[scheduler] Post-peak battery mode switch (peak ended %02d:00)", peak["end_h"])
    _last_mode_switch = await battery_mode.switch_to_savings()


async def _apply_overnight_decision(enabled: bool, reasoning: str) -> dict | None:
    """
    Push the overnight-charging decision to the JuiceBox right now.

    Shared by `_nightly_calendar_check` (21:00 scheduler) and the
    `set_overnight_mode` MCP tool so both paths have the same "decide →
    immediately program JuiceBox" behavior. The 04:00 daily run is the
    safety-net / idempotent retry.

      enabled=True  → coordinator.run()                          (push TOU)
      enabled=False → juicebox_mcp.set_charging_schedule([])     (clear)

    Returns the updated _last_result dict on success, or None on push
    failure (caller logs; 04:00 run retries).
    """
    global _last_result
    try:
        if enabled:
            log.info("[overnight] Pushing TOU schedule to JuiceBox — %s", reasoning)
            _last_result = await coordinator.run()
            log.info("[overnight] JuiceBox programmed — status: %s", _last_result.get("status"))
        else:
            log.info("[overnight] Clearing JuiceBox schedule (surplus-only) — %s", reasoning)
            jb_resp = await juicebox_mcp.set_charging_schedule([])
            now_iso = datetime.now(ARIZONA).isoformat()
            _last_result = {
                "started_at":        now_iso,
                "status":            "ok",
                "schedule":          [],
                "reasoning":         reasoning,
                "juicebox_ok":       True,
                "juicebox_response": jb_resp,
                "errors":            [],
                "finished_at":       now_iso,
            }
        return _last_result
    except Exception as exc:
        log.error(
            "[overnight] Failed to update JuiceBox: %s  (04:00 safety-net run will retry)",
            exc,
        )
        return None


async def _nightly_calendar_check() -> None:
    """
    Runs at 21:00 Arizona. Fetches tomorrow's calendar events, estimates total
    driving distance, sets _overnight_charging, and pushes the resulting
    schedule to the JuiceBox immediately so overnight charging can actually
    start at 22:00 instead of waiting for the 04:00 safety-net run (by which
    time the morning commute is only a few hours away).

    Flow:
      enabled  → coordinator.run()            (fetch tariff + push TOU schedule)
      disabled → juicebox_mcp.set_charging_schedule([])  (clear — surplus-only)

    The 04:00 daily run re-asserts whichever schedule the flag calls for, so a
    21:00 push failure is self-healing. No iCal URLs configured → stay disabled.
    """
    global _overnight_charging
    ical_urls = [u.strip() for u in os.getenv("GOOGLE_ICAL_URLS", "").split(",") if u.strip()]

    if not ical_urls:
        log.info("[calendar_check] GOOGLE_ICAL_URLS not configured — overnight charging stays disabled (surplus-only mode)")
        return

    log.info("[calendar_check] Running nightly calendar check (%d feed(s))", len(ical_urls))
    try:
        result = await calendar_check.check_tomorrow_driving(ical_urls)
    except Exception as exc:
        log.error("[calendar_check] Check failed — leaving overnight charging disabled (surplus-only): %s", exc)
        return

    enabled = result["overnight_charging_needed"]
    _overnight_charging = {
        "enabled":         enabled,
        "reason":          result["reasoning"],
        "set_at":          datetime.now(ARIZONA).isoformat(),
        "calendar_result": result,
    }
    log.info("[calendar_check] %s", result["reasoning"])

    await _apply_overnight_decision(
        enabled,
        reasoning=f"21:00 calendar check: {result['reasoning']}",
    )


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
    """Restore the base schedule and reset surplus state.

    Uses _last_result["schedule"] (which may be [] when overnight is disabled —
    surplus-only mode). Only falls back to recomputing if _last_result has no
    schedule key at all (e.g. first boot before any run has completed).
    """
    global _surplus_state
    if _last_result is not None and "schedule" in _last_result:
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
    # Mode-switch jobs: weekdays only (APS peak is weekday-only).
    # Times seeded from APS default (16:00–19:00 → 15:57 / 19:02); reschedule
    # happens after every tariff refresh using the actual peak window.
    initial = _peak_switch_times({})
    scheduler.add_job(
        _scheduled_pre_peak_mode_switch,
        "cron",
        day_of_week="mon-fri",
        hour=initial["pre_h"],
        minute=initial["pre_m"],
        id="battery_mode_pre_peak",
    )
    scheduler.add_job(
        _scheduled_post_peak_mode_switch,
        "cron",
        day_of_week="mon-fri",
        hour=initial["post_h"],
        minute=initial["post_m"],
        id="battery_mode_post_peak",
    )
    return scheduler

# ── Entry point ───────────────────────────────────────────────────────────────

async def _run_stdio():
    global _scheduler
    scheduler = _build_scheduler()
    _scheduler = scheduler
    scheduler.start()
    next_daily      = scheduler.get_job("daily_coordinator").next_run_time
    next_report     = scheduler.get_job("weekly_report").next_run_time
    next_surplus    = scheduler.get_job("surplus_monitor").next_run_time
    next_calendar   = scheduler.get_job("calendar_check").next_run_time
    next_pre_peak   = scheduler.get_job("battery_mode_pre_peak").next_run_time
    next_post_peak  = scheduler.get_job("battery_mode_post_peak").next_run_time
    log.info("Coordinator MCP server starting (stdio)")
    log.info("  Daily scheduler:  next run at %s (America/Phoenix)", next_daily)
    log.info("  Weekly report:    next run at %s (America/Phoenix)", next_report)
    log.info("  Surplus monitor:  next run at %s (America/Phoenix)", next_surplus)
    log.info("  Calendar check:   next run at %s (America/Phoenix)", next_calendar)
    log.info("  Pre-peak switch:  next run at %s (America/Phoenix)", next_pre_peak)
    log.info("  Post-peak switch: next run at %s (America/Phoenix)", next_post_peak)
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
        global _scheduler
        scheduler = _build_scheduler()
        _scheduler = scheduler
        scheduler.start()
        next_daily      = scheduler.get_job("daily_coordinator").next_run_time
        next_report     = scheduler.get_job("weekly_report").next_run_time
        next_surplus    = scheduler.get_job("surplus_monitor").next_run_time
        next_calendar   = scheduler.get_job("calendar_check").next_run_time
        next_pre_peak   = scheduler.get_job("battery_mode_pre_peak").next_run_time
        next_post_peak  = scheduler.get_job("battery_mode_post_peak").next_run_time
        log.info("Coordinator MCP server starting (SSE) on %s:%d", host, port)
        log.info("  Daily scheduler:  next run at %s (America/Phoenix)", next_daily)
        log.info("  Weekly report:    next run at %s (America/Phoenix)", next_report)
        log.info("  Surplus monitor:  next run at %s (America/Phoenix)", next_surplus)
        log.info("  Calendar check:   next run at %s (America/Phoenix)", next_calendar)
        log.info("  Pre-peak switch:  next run at %s (America/Phoenix)", next_pre_peak)
        log.info("  Post-peak switch: next run at %s (America/Phoenix)", next_post_peak)
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
