"""
Surplus solar detection.

Determines when the house battery is full and solar production exceeds home
consumption, signalling that excess energy should charge the car rather than
export to the grid.

Pure functions — no async, no I/O. Server.py owns the polling loop and state.
"""

import logging

log = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

BATTERY_FULL_SOC = 95       # % — treat battery as "full" at or above this
BATTERY_LOW_SOC  = 88       # % — below this, stop surplus charging (battery draining)
SURPLUS_MIN_W    = 400      # Minimum net solar excess watts to activate charging
ACTIVATION_POLLS   = 2      # Consecutive surplus readings required to activate
DEACTIVATION_POLLS = 2      # Consecutive non-surplus readings required to deactivate

PEAK_BUFFER_MIN  = 15       # Safety buffer around peak window (each side), minutes

MIN_CHARGE_AMPS  = 6        # JuiceBox minimum below which the charger won't engage
MAX_CHARGE_AMPS  = 32       # JuiceBox maximum
CHARGE_VOLTAGE   = 240      # Level 2 charging voltage


# ── Data extraction ───────────────────────────────────────────────────────────

def extract_current_values(summary: dict) -> dict:
    """
    Pull battery SOC, production, and consumption from an
    enphase_get_energy_summary response.

    Returns a dict with keys:
      battery_soc      int | None   — percent (0–100)
      production_w     int          — most recent 15-min solar production, watts
      consumption_w    int          — most recent 15-min home consumption, watts
      solar_grid_w     int          — solar currently exported to grid, watts
    """
    try:
        stats      = summary["today_stats"]["stats"][0]
        battery_soc = summary["today_stats"]["battery_details"]["aggregate_soc"]

        def _last_nonnull(arr) -> int:
            vals = [x for x in arr if x is not None]
            return int(vals[-1]) if vals else 0

        return {
            "battery_soc":   battery_soc,
            "production_w":  _last_nonnull(stats.get("production",  [])),
            "consumption_w": _last_nonnull(stats.get("consumption", [])),
            "solar_grid_w":  _last_nonnull(stats.get("solar_grid",  [])),
        }
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("[surplus_monitor] Failed to extract values from summary: %s", exc)
        return {"battery_soc": None, "production_w": 0, "consumption_w": 0, "solar_grid_w": 0}


# ── Decision functions ────────────────────────────────────────────────────────

def is_surplus(values: dict) -> bool:
    """
    True when the battery is full and solar is producing more than the house
    consumes — meaning the excess would otherwise go to the grid.
    """
    soc = values.get("battery_soc")
    if soc is None:
        return False
    net = values.get("production_w", 0) - values.get("consumption_w", 0)
    return soc >= BATTERY_FULL_SOC and net >= SURPLUS_MIN_W


def is_no_longer_surplus(values: dict) -> bool:
    """
    True when surplus charging should stop: battery is depleting or
    solar production has dropped below home consumption.
    """
    soc = values.get("battery_soc")
    if soc is None:
        return True  # can't read — safer to stop
    net = values.get("production_w", 0) - values.get("consumption_w", 0)
    return soc < BATTERY_LOW_SOC or net < 0


# ── Amp calculation ───────────────────────────────────────────────────────────

def compute_charge_amps(surplus_w: int) -> int:
    """
    Convert net solar excess watts to JuiceBox charge amps.
    Clamped to [MIN_CHARGE_AMPS, MAX_CHARGE_AMPS].
    """
    amps = surplus_w // CHARGE_VOLTAGE
    return max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, amps))


# ── Peak-time guard ───────────────────────────────────────────────────────────

def is_peak_time(hour: int, minute: int, peak_start_h: int, peak_end_h: int) -> bool:
    """
    True if the current time falls within the peak pricing window plus a
    PEAK_BUFFER_MIN safety margin on each side — guards against small clock
    drift between this server and the utility meter.

    E.g. peak 16:00–19:00 with 15-min buffer → blocked 15:45–19:15.
    Handles non-wraparound windows only (peak never crosses midnight in APS TOU).
    """
    current_min       = hour * 60 + minute
    buffered_start    = peak_start_h * 60 - PEAK_BUFFER_MIN
    buffered_end      = peak_end_h   * 60 + PEAK_BUFFER_MIN
    if buffered_start < buffered_end:
        return buffered_start <= current_min < buffered_end
    # Wraparound case (defensive — unlikely for APS peak)
    return current_min >= buffered_start or current_min < buffered_end
