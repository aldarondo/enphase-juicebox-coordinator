"""
Compute the JuiceBox charging schedule from an Enphase TOU tariff.

Simple approach: find the most expensive (peak) rate window on weekdays, then
charge during everything else. Weekends have no meaningful peak so they get a
wide window.

Supports the Enphase Enlighten app-api tariff format:
  purchase.seasons[].days[id="weekdays"].periods[type="peak"]
  startTime/endTime are minutes from midnight (e.g. 960=16:00, 1139=18:59).

If the tariff can't be parsed, falls back to APS defaults (on-peak 16:00–19:00
weekdays) — accurate for APS R-3 TOU 4pm-7pm Weekdays plan.
"""

import logging
from datetime import date

log = logging.getLogger(__name__)

# APS R-3 TOU fallback — on-peak window (4pm-7pm weekdays)
APS_DEFAULT_PEAK = {"start_h": 16, "end_h": 19, "source": "APS default (16:00–19:00 weekdays)"}


def _active_season(seasons: list) -> dict | None:
    """Pick the season whose month range covers today."""
    today_month = date.today().month

    # Enphase format: explicit endMonth present → range-based matching
    has_end_months = any(
        s.get("endMonth") is not None or s.get("end_month") is not None
        for s in seasons
    )

    if has_end_months:
        for s in seasons:
            try:
                start = int(s.get("startMonth", s.get("start_month", 1)))
                end   = int(s.get("endMonth",   s.get("end_month",   12)))
            except (TypeError, ValueError):
                continue
            # Handle wraparound (e.g. Nov–Apr: start=11, end=4)
            if start <= end:
                if start <= today_month <= end:
                    return s
            else:
                if today_month >= start or today_month <= end:
                    return s
        return seasons[0] if seasons else None

    # Legacy format: only start_month → return last season that started by today
    best = None
    for s in seasons:
        try:
            start = int(s.get("startMonth", s.get("start_month", 1)))
        except (TypeError, ValueError):
            continue
        if start <= today_month:
            best = s
    return best or (seasons[0] if seasons else None)


def _find_peak_weekday_hours(tariff: dict) -> dict | None:
    """
    Return {"start_h": int, "end_h": int, "source": str} for the weekday
    peak window, or None if the tariff can't be parsed.
    """
    # Enphase app-api format: top-level key is "purchase"
    # Legacy / test fixture format: top-level key is "tariff" / "tariff_plan" or bare dict
    section = tariff.get("purchase") or tariff.get("tariff") or tariff.get("tariff_plan") or tariff
    if not isinstance(section, dict):
        return None

    seasons = section.get("seasons", [])
    if not seasons:
        return None

    active = _active_season(seasons)
    if not active:
        return None

    log.debug("[optimizer] Active season: %s", active.get("id") or active.get("season", "?"))

    # --- Enphase app-api format -------------------------------------------
    # season.days[] has {id, days, periods[]}; we want id == "weekdays"
    for day_type in active.get("days", []):
        if day_type.get("id") != "weekdays":
            continue
        for period in day_type.get("periods", []):
            if period.get("type") != "peak":
                continue
            s_min = period.get("startTime", "")
            e_min = period.get("endTime",   "")
            if s_min == "" or e_min == "":
                continue
            try:
                start_h = int(s_min) // 60
                # endTime is the last minute of the period (inclusive); next hour is end_h
                end_h   = (int(e_min) + 1) // 60
            except (TypeError, ValueError):
                continue
            if start_h == end_h:
                continue
            name = period.get("id", "peak")
            rate = period.get("rate", "?")
            log.info("[optimizer] Peak period '%s': %02d:00–%02d:00 @ $%s/kWh",
                     name, start_h, end_h, rate)
            return {"start_h": start_h, "end_h": end_h, "source": f"tariff period '{name}'"}

    # --- Legacy test-fixture format ----------------------------------------
    # season.tou_periods[] has {id, buy, charge_periods[{day_types, start, end}]}
    periods = active.get("tou_periods") or active.get("periods") or []
    if not periods:
        return None

    def rate(p):
        for k in ("buy", "rate", "price", "import_rate"):
            v = p.get(k)
            if v is not None:
                try: return float(v)
                except (TypeError, ValueError): pass
        return 0.0

    peak = max(periods, key=rate)
    for r in (peak.get("charge_periods") or peak.get("ranges") or []):
        day_types = r.get("day_types") or r.get("days") or ["weekdays"]
        if isinstance(day_types, str):
            day_types = [day_types]
        if not any("week" in d.lower() and "end" not in d.lower() for d in day_types):
            continue
        try:
            start_h = int(r.get("start", r.get("from", r.get("start_hour", 0))))
            end_h   = int(r.get("end",   r.get("to",   r.get("end_hour",   0))))
        except (TypeError, ValueError):
            continue
        if start_h != end_h:
            name = peak.get("id") or peak.get("name", "peak")
            return {"start_h": start_h, "end_h": end_h,
                    "source": f"tariff period '{name}'"}

    return None


def _find_daytime_window(tariff: dict, peak: dict) -> dict:
    """
    Find the cheapest contiguous daytime charging window for overnight-disabled
    mode (no long trip tomorrow — surplus solar + cheapest rate only).

    Scans for uncovered minutes in the 10:00–peak_start range.  In winter this
    yields the APS super off-peak gap (10:00–15:00).  In summer (no super
    off-peak period), the whole 10:00–peak_start window is returned.

    Returns {"start": "HH:MM", "end": "HH:MM"}.
    """
    fallback = {"start": "10:00", "end": f"{peak['start_h']:02d}:00"}

    section = tariff.get("purchase") or tariff.get("tariff") or tariff.get("tariff_plan") or tariff
    if not isinstance(section, dict):
        return fallback

    seasons = section.get("seasons", [])
    active  = _active_season(seasons)
    if not active:
        return fallback

    # Collect every minute explicitly covered by a named weekday period.
    covered: set[int] = set()
    for day_type in active.get("days", []):
        if day_type.get("id") != "weekdays":
            continue
        for period in day_type.get("periods", []):
            s = period.get("startTime", "")
            e = period.get("endTime",   "")
            if s == "" or e == "":
                continue
            try:
                covered.update(range(int(s), int(e) + 1))
            except (TypeError, ValueError):
                continue

    # Search for uncovered gaps between 10:00 and peak_start.
    search_end = peak["start_h"] * 60 - 1
    gaps = [m for m in range(600, search_end + 1) if m not in covered]

    if not gaps:
        return fallback

    # Single contiguous gap expected (e.g. 600–899 for APS winter).
    start_h, start_m = divmod(gaps[0],      60)
    end_h,   end_m   = divmod(gaps[-1] + 1, 60)
    return {"start": f"{start_h:02d}:{start_m:02d}", "end": f"{end_h:02d}:{end_m:02d}"}


def compute_schedule(tariff: dict, overnight_enabled: bool = True) -> tuple[list[dict], str]:
    """
    Return (schedule, reasoning) where schedule is ready for
    the JuiceBox set_charging_schedule tool.

    overnight_enabled=True  (long trip tomorrow or default):
      Weekdays: charge from peak_end → peak_start (all non-peak hours, wraps overnight).

    overnight_enabled=False (no long trip — surplus solar / cheap rate only):
      Weekdays: charge only during the cheapest daytime window (super off-peak
      when available, otherwise 10:00→peak_start).  No overnight draw.

    Weekends always get a wide window (no meaningful peak pricing).
    """
    peak = _find_peak_weekday_hours(tariff)

    if peak:
        reasoning = (f"Peak window from tariff: {peak['start_h']:02d}:00–"
                     f"{peak['end_h']:02d}:00 weekdays ({peak['source']})")
    else:
        peak      = APS_DEFAULT_PEAK
        reasoning = f"Tariff not parsed — using {peak['source']}"

    log.info("[optimizer] %s", reasoning)

    if overnight_enabled:
        weekday_window = {
            "label":    f"Weekday off-peak — avoid {peak['start_h']:02d}:00–{peak['end_h']:02d}:00",
            "start":    f"{peak['end_h']:02d}:00",
            "end":      f"{peak['start_h']:02d}:00",
            "max_amps": 32,
        }
        reasoning += " | overnight charging enabled"
    else:
        dw = _find_daytime_window(tariff, peak)
        weekday_window = {
            "label":    f"Weekday super off-peak {dw['start']}–{dw['end']} (overnight disabled)",
            "start":    dw["start"],
            "end":      dw["end"],
            "max_amps": 32,
        }
        reasoning += f" | overnight disabled — daytime only {dw['start']}–{dw['end']}"

    log.info("[optimizer] %s", reasoning)

    schedule = [
        {**weekday_window, "days": ["mon", "tue", "wed", "thu", "fri"]},
        {
            "label":    "Weekend — no peak pricing",
            "days":     ["sat", "sun"],
            "start":    "08:00",
            "end":      "22:00",
            "max_amps": 32,
        },
    ]

    return schedule, reasoning
