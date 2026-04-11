"""
Compute the JuiceBox charging schedule from an Enphase TOU tariff.

Simple approach: find the most expensive (peak) rate period, then charge
during everything else. Weekends have no peak period so they're unrestricted.

If the tariff can't be parsed, fall back to APS defaults (on-peak 15:00–20:00
weekdays) — accurate for most Arizona APS TOU plans.
"""

import logging
from datetime import date

log = logging.getLogger(__name__)

# APS fallback — on-peak window for Saver Choice / Saver Choice Plus
APS_DEFAULT_PEAK = {"start_h": 15, "end_h": 20, "source": "APS default (15:00–20:00 weekdays)"}


def _find_peak_weekday_hours(tariff: dict) -> dict | None:
    """
    Return the on-peak window as {"start_h": int, "end_h": int, "source": str}.
    Returns None if the tariff structure can't be parsed.
    """
    t = tariff.get("tariff") or tariff.get("tariff_plan") or tariff

    seasons = t.get("seasons", [])
    if not seasons:
        return None

    # Pick the season whose start month is ≤ today (last one wins)
    today_month = date.today().month
    active = seasons[0]
    for s in sorted(seasons, key=lambda x: x.get("start_month") or x.get("season_start_month", 1)):
        if (s.get("start_month") or s.get("season_start_month", 1)) <= today_month:
            active = s

    periods = active.get("tou_periods") or active.get("periods") or []
    if not periods:
        return None

    # The peak period has the highest buy rate
    def rate(p):
        for k in ("buy", "rate", "price", "import_rate"):
            v = p.get(k)
            if v is not None:
                try: return float(v)
                except (TypeError, ValueError): pass
        return 0.0

    peak = max(periods, key=rate)
    log.info("[optimizer] Peak period: '%s' @ %.4f $/kWh",
             peak.get("id") or peak.get("name", "?"), rate(peak))

    # Extract the weekday hour range from this period
    ranges = (
        peak.get("charge_periods") or peak.get("ranges") or
        peak.get("periods_per_day") or []
    )
    for r in ranges:
        day_types = r.get("day_types") or r.get("days") or ["weekdays"]
        if isinstance(day_types, str):
            day_types = [day_types]
        is_weekday = any("week" in d.lower() and "end" not in d.lower() for d in day_types)
        if not is_weekday:
            continue
        start_h = int(r.get("start", r.get("from", r.get("start_hour", 0))))
        end_h   = int(r.get("end",   r.get("to",   r.get("end_hour",   0))))
        if start_h != end_h:
            name = peak.get("id") or peak.get("name", "peak")
            return {"start_h": start_h, "end_h": end_h,
                    "source": f"tariff period '{name}'"}

    return None


def compute_schedule(tariff: dict) -> tuple[list[dict], str]:
    """
    Return (schedule, reasoning) where schedule is ready for
    the JuiceBox set_charging_schedule tool.

    Logic:
      - Weekdays: charge from peak_end → peak_start (one overnight window)
      - Weekends: charge 08:00–22:00 (no peak pricing on weekends)
    """
    peak = _find_peak_weekday_hours(tariff)

    if peak:
        reasoning = f"Peak window from tariff: {peak['start_h']:02d}:00–{peak['end_h']:02d}:00 weekdays ({peak['source']})"
    else:
        peak = APS_DEFAULT_PEAK
        reasoning = f"Tariff not parsed — using {peak['source']}"

    log.info("[optimizer] %s", reasoning)

    schedule = [
        {
            "label":    f"Weekday off-peak — avoid {peak['start_h']:02d}:00–{peak['end_h']:02d}:00",
            "days":     ["mon", "tue", "wed", "thu", "fri"],
            "start":    f"{peak['end_h']:02d}:00",   # charge starts when peak ends
            "end":      f"{peak['start_h']:02d}:00",  # charge stops when peak begins
            "max_amps": 32,
        },
        {
            "label":    "Weekend — no peak pricing",
            "days":     ["sat", "sun"],
            "start":    "08:00",
            "end":      "22:00",
            "max_amps": 32,
        },
    ]

    return schedule, reasoning
