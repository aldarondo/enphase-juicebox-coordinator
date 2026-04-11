"""
Core optimization logic: parse the Enphase TOU tariff and compute
the optimal JuiceBox charging schedule for the week.

Strategy
--------
1. Find the cheapest rate tier in the active season's TOU schedule.
2. Map its applicable hours to weekday and weekend charging windows.
3. Scale max amps by battery SOC — if the home battery is low, throttle
   the EV so the battery can recharge from solar during the day.
4. Fall back to conservative Arizona TOU defaults if parsing fails.

Output format matches JuiceBox MCP's set_charging_schedule tool input.
"""

import logging
from datetime import date

log = logging.getLogger(__name__)

# ─── SOC → amps mapping ────────────────────────────────────────────────────────
# Battery SOC directly affects how aggressively we can charge the EV.
# If SOC is low, the home battery needs to recharge from solar; pulling
# 32A for the EV at the same time stresses the system unnecessarily.

def _amps_for_soc(soc: float) -> int:
    if soc >= 70:  return 32   # battery healthy — full 32A for EV
    if soc >= 40:  return 24   # moderate — ease off slightly
    return 16                  # battery is low — EV takes a back seat

# ─── Defaults (conservative Arizona TOU) ──────────────────────────────────────
# Used when tariff parsing fails. These cover the off-peak window for most
# Arizona utilities (APS, SRP, TEP) without touching any on-peak period.

def _default_windows(amps: int) -> list[dict]:
    return [
        {
            "label":    "Weekday overnight off-peak (default)",
            "days":     ["mon", "tue", "wed", "thu", "fri"],
            "start":    "23:00",
            "end":      "07:00",
            "max_amps": amps,
        },
        {
            "label":    "Weekend solar hours (default)",
            "days":     ["sat", "sun"],
            "start":    "09:00",
            "end":      "17:00",
            "max_amps": amps,
        },
    ]

# ─── Tariff parsing ────────────────────────────────────────────────────────────

def _active_season(seasons: list) -> dict | None:
    """Return the season whose start month is ≤ today's month (last match wins)."""
    today_month = date.today().month
    active = None
    for s in sorted(seasons, key=lambda x: x.get("start_month") or x.get("season_start_month", 1)):
        start = s.get("start_month") or s.get("season_start_month", 1)
        if start <= today_month:
            active = s
    return active or (seasons[0] if seasons else None)


def _period_rate(period: dict) -> float:
    """Extract the buy rate from a TOU period, trying several field names."""
    for key in ("buy", "rate", "price", "import_rate", "cost"):
        val = period.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return float("inf")


def _extract_hour_ranges(period: dict) -> dict:
    """
    Extract weekday/weekend hour ranges from a TOU period.
    Returns {"weekday": [(start, end), ...], "weekend": [(start, end), ...]}.
    Tries several field layouts used by different Enphase tariff versions.
    """
    weekday, weekend = [], []

    raw_ranges = (
        period.get("charge_periods") or
        period.get("ranges") or
        period.get("periods_per_day") or
        period.get("hours") or
        []
    )

    for rng in raw_ranges:
        start_h = int(rng.get("start", rng.get("from", rng.get("start_hour", 0))))
        end_h   = int(rng.get("end",   rng.get("to",   rng.get("end_hour",   0))))
        if start_h == end_h:
            continue

        day_types = (
            rng.get("day_types") or
            rng.get("days") or
            rng.get("applies_to") or
            ["weekdays", "weekends"]
        )
        if isinstance(day_types, str):
            day_types = [day_types]

        for dt in day_types:
            dt_lower = dt.lower()
            if "weekend" in dt_lower or "sat" in dt_lower or "sun" in dt_lower:
                weekend.append((start_h, end_h))
            else:
                weekday.append((start_h, end_h))

    return {"weekday": weekday, "weekend": weekend}


def _parse_tariff(tariff: dict) -> dict | None:
    """
    Parse the Enphase tariff JSON and return the cheap-rate hour ranges.
    Returns None if the structure can't be interpreted.
    """
    # Unwrap common top-level keys
    t = tariff.get("tariff") or tariff.get("tariff_plan") or tariff

    seasons = t.get("seasons", [])
    if not seasons:
        log.warning("[optimizer] No seasons in tariff — will use defaults")
        return None

    season = _active_season(seasons)
    if not season:
        log.warning("[optimizer] Could not determine active season — will use defaults")
        return None

    log.info("[optimizer] Active season: %s (start month %s)",
             season.get("name", "?"),
             season.get("start_month") or season.get("season_start_month", "?"))

    tou_periods = (
        season.get("tou_periods") or
        season.get("periods") or
        season.get("schedules") or
        []
    )
    if not tou_periods:
        log.warning("[optimizer] No TOU periods in active season — will use defaults")
        return None

    # Log all periods and their rates for transparency
    for p in tou_periods:
        log.info("[optimizer] Period '%s': %.4f $/kWh",
                 p.get("id") or p.get("name", "?"),
                 _period_rate(p))

    cheapest = min(tou_periods, key=_period_rate)
    log.info("[optimizer] Cheapest period: '%s' @ %.4f $/kWh",
             cheapest.get("id") or cheapest.get("name", "?"),
             _period_rate(cheapest))

    ranges = _extract_hour_ranges(cheapest)
    if not ranges["weekday"] and not ranges["weekend"]:
        log.warning("[optimizer] Cheapest period has no parseable hour ranges — will use defaults")
        return None

    return ranges


# ─── Public entry point ────────────────────────────────────────────────────────

def compute_schedule(tariff: dict, battery_soc: float) -> tuple[list[dict], str]:
    """
    Compute the optimal JuiceBox charging schedule.

    Args:
        tariff:      Raw JSON from Enphase enphase_get_tariff / tariff.json endpoint.
        battery_soc: Current home battery state-of-charge as a percentage (0–100).

    Returns:
        (schedule, reasoning)
        schedule  — list of window dicts ready for JuiceBox set_charging_schedule
        reasoning — human-readable explanation of decisions made
    """
    amps = _amps_for_soc(battery_soc)
    log.info("[optimizer] Battery SOC %.0f%% → %dA max for EV charging", battery_soc, amps)

    cheap_hours = _parse_tariff(tariff)

    if not cheap_hours:
        windows = _default_windows(amps)
        reasoning = (
            f"Tariff could not be parsed — using Arizona TOU defaults "
            f"(weekday 23:00–07:00, weekend 09:00–17:00). "
            f"Battery SOC {battery_soc:.0f}% → {amps}A."
        )
        log.info("[optimizer] %s", reasoning)
        return windows, reasoning

    windows = []
    parts   = []

    # Weekday windows
    wd_ranges = cheap_hours["weekday"]
    if wd_ranges:
        for s, e in wd_ranges:
            windows.append({
                "label":    f"Weekday off-peak {s:02d}:00–{e:02d}:00",
                "days":     ["mon", "tue", "wed", "thu", "fri"],
                "start":    f"{s:02d}:00",
                "end":      f"{e:02d}:00",
                "max_amps": amps,
            })
        parts.append("Weekday windows: " + ", ".join(f"{s:02d}:00–{e:02d}:00" for s, e in wd_ranges))
    else:
        windows.append({
            "label":    "Weekday overnight (fallback)",
            "days":     ["mon", "tue", "wed", "thu", "fri"],
            "start":    "23:00",
            "end":      "07:00",
            "max_amps": amps,
        })
        parts.append("No weekday windows in tariff — fallback 23:00–07:00")

    # Weekend windows
    we_ranges = cheap_hours["weekend"]
    if we_ranges:
        for s, e in we_ranges:
            windows.append({
                "label":    f"Weekend {s:02d}:00–{e:02d}:00",
                "days":     ["sat", "sun"],
                "start":    f"{s:02d}:00",
                "end":      f"{e:02d}:00",
                "max_amps": amps,
            })
        parts.append("Weekend windows: " + ", ".join(f"{s:02d}:00–{e:02d}:00" for s, e in we_ranges))
    else:
        windows.append({
            "label":    "Weekend solar hours (fallback)",
            "days":     ["sat", "sun"],
            "start":    "09:00",
            "end":      "17:00",
            "max_amps": amps,
        })
        parts.append("No weekend windows in tariff — fallback solar hours 09:00–17:00")

    parts.append(f"Battery SOC {battery_soc:.0f}% → {amps}A max")
    reasoning = ". ".join(parts) + "."
    log.info("[optimizer] Schedule computed: %d windows. %s", len(windows), reasoning)
    return windows, reasoning
