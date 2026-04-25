"""
Tests for optimizer.py — pure functions, no I/O, no mocking needed.
"""

import sys
import os
import pytest
from datetime import date

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer import _find_peak_weekday_hours, _find_daytime_window, compute_schedule

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

SAMPLE_TARIFF = {
    "tariff": {
        "seasons": [
            {
                "start_month": 1,
                "tou_periods": [
                    {
                        "id": "off_peak",
                        "buy": 0.09,
                        "charge_periods": [
                            {"day_types": ["weekdays"], "start": 0, "end": 15}
                        ],
                    },
                    {
                        "id": "on_peak",
                        "buy": 0.28,
                        "charge_periods": [
                            {"day_types": ["weekdays"], "start": 15, "end": 20}
                        ],
                    },
                    {
                        "id": "weekend",
                        "buy": 0.09,
                        "charge_periods": [
                            {"day_types": ["weekends"], "start": 0, "end": 24}
                        ],
                    },
                ],
            }
        ]
    }
}


# ===========================================================================
# _find_peak_weekday_hours tests
# ===========================================================================

class TestFindPeakWeekdayHours:

    def test_returns_on_peak_window_from_sample_tariff(self):
        result = _find_peak_weekday_hours(SAMPLE_TARIFF)
        assert result is not None
        assert result["start_h"] == 15
        assert result["end_h"] == 20

    def test_alternate_field_names_tariff_plan_and_periods(self):
        """Supports tariff_plan / periods / ranges / from+to / days / rate."""
        tariff = {
            "tariff_plan": {
                "seasons": [
                    {
                        "start_month": 1,
                        "periods": [
                            {
                                "id": "cheap",
                                "rate": 0.09,
                                "ranges": [
                                    {"days": ["weekdays"], "from": 0, "to": 10}
                                ],
                            },
                            {
                                "id": "peak",
                                "rate": 0.35,
                                "ranges": [
                                    {"days": ["weekdays"], "from": 16, "to": 21}
                                ],
                            },
                        ],
                    }
                ]
            }
        }
        result = _find_peak_weekday_hours(tariff)
        assert result is not None
        assert result["start_h"] == 16
        assert result["end_h"] == 21

    def test_alternate_field_name_price(self):
        tariff = {
            "tariff": {
                "seasons": [
                    {
                        "start_month": 1,
                        "tou_periods": [
                            {
                                "id": "low",
                                "price": 0.08,
                                "charge_periods": [
                                    {"day_types": ["weekdays"], "start": 0, "end": 12}
                                ],
                            },
                            {
                                "id": "high",
                                "price": 0.40,
                                "charge_periods": [
                                    {"day_types": ["weekdays"], "start": 14, "end": 19}
                                ],
                            },
                        ],
                    }
                ]
            }
        }
        result = _find_peak_weekday_hours(tariff)
        assert result is not None
        assert result["start_h"] == 14
        assert result["end_h"] == 19

    def test_alternate_field_name_import_rate(self):
        tariff = {
            "tariff": {
                "seasons": [
                    {
                        "start_month": 1,
                        "tou_periods": [
                            {
                                "id": "low",
                                "import_rate": 0.07,
                                "charge_periods": [
                                    {"day_types": ["weekdays"], "start": 0, "end": 13}
                                ],
                            },
                            {
                                "id": "peak",
                                "import_rate": 0.45,
                                "charge_periods": [
                                    {"day_types": ["weekdays"], "start": 13, "end": 18}
                                ],
                            },
                        ],
                    }
                ]
            }
        }
        result = _find_peak_weekday_hours(tariff)
        assert result is not None
        assert result["start_h"] == 13
        assert result["end_h"] == 18

    def test_skips_weekend_only_periods(self):
        """A period whose only day_type is 'weekends' must not be returned."""
        tariff = {
            "tariff": {
                "seasons": [
                    {
                        "start_month": 1,
                        "tou_periods": [
                            {
                                "id": "weekend_peak",
                                "buy": 0.99,
                                "charge_periods": [
                                    # Only weekends — should be skipped
                                    {"day_types": ["weekends"], "start": 10, "end": 18}
                                ],
                            },
                            {
                                "id": "weekday_only",
                                "buy": 0.20,
                                "charge_periods": [
                                    {"day_types": ["weekdays"], "start": 15, "end": 20}
                                ],
                            },
                        ],
                    }
                ]
            }
        }
        # The highest-rate period is weekend_peak (0.99) but it has no weekday
        # ranges, so the function should fall through to None for *that* period
        # and return None (the weekday_only period is not the max).
        result = _find_peak_weekday_hours(tariff)
        # weekend_peak wins by rate but has no weekday range → returns None
        assert result is None

    def test_returns_none_when_seasons_empty(self):
        tariff = {"tariff": {"seasons": []}}
        assert _find_peak_weekday_hours(tariff) is None

    def test_returns_none_when_periods_empty(self):
        tariff = {
            "tariff": {
                "seasons": [
                    {"start_month": 1, "tou_periods": []}
                ]
            }
        }
        assert _find_peak_weekday_hours(tariff) is None

    def test_returns_none_on_completely_empty_dict(self):
        assert _find_peak_weekday_hours({}) is None

    def test_selects_active_season_by_month(self, monkeypatch):
        """
        Two seasons: start_month=1 and start_month=6.
        When today's month >= 6, the second season should be active.
        When today's month < 6, the first season should be active.
        """
        tariff = {
            "tariff": {
                "seasons": [
                    {
                        "start_month": 1,
                        "tou_periods": [
                            {
                                "id": "winter_peak",
                                "buy": 0.20,
                                "charge_periods": [
                                    {"day_types": ["weekdays"], "start": 7, "end": 11}
                                ],
                            }
                        ],
                    },
                    {
                        "start_month": 6,
                        "tou_periods": [
                            {
                                "id": "summer_peak",
                                "buy": 0.35,
                                "charge_periods": [
                                    {"day_types": ["weekdays"], "start": 15, "end": 20}
                                ],
                            }
                        ],
                    },
                ]
            }
        }

        # Simulate a month inside the summer season (month 7)
        monkeypatch.setattr(
            "optimizer.date",
            type("FakeDate", (), {"today": staticmethod(lambda: date(2025, 7, 1))})(),
        )
        result = _find_peak_weekday_hours(tariff)
        assert result is not None
        assert result["start_h"] == 15
        assert result["end_h"] == 20

        # Simulate a month inside the winter season (month 3)
        monkeypatch.setattr(
            "optimizer.date",
            type("FakeDate", (), {"today": staticmethod(lambda: date(2025, 3, 1))})(),
        )
        result = _find_peak_weekday_hours(tariff)
        assert result is not None
        assert result["start_h"] == 7
        assert result["end_h"] == 11


# ===========================================================================
# compute_schedule tests
# ===========================================================================

class TestComputeSchedule:

    def test_returns_exactly_two_entries(self):
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        assert len(schedule) == 2

    def test_weekday_window_start_and_end(self):
        """Weekday window: start=peak_end, end=peak_start."""
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        weekday = next(e for e in schedule if "mon" in e["days"])
        # peak 15–20 → charge from 20:00 to 15:00
        assert weekday["start"] == "20:00"
        assert weekday["end"] == "15:00"

    def test_weekday_days_are_mon_through_fri(self):
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        weekday = next(e for e in schedule if "mon" in e["days"])
        assert set(weekday["days"]) == {"mon", "tue", "wed", "thu", "fri"}

    def test_weekday_max_amps_is_32(self):
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        weekday = next(e for e in schedule if "mon" in e["days"])
        assert weekday["max_amps"] == 32

    def test_weekend_window_is_full_day_when_overnight_enabled(self):
        """Weekend window is 00:00–23:59 when overnight enabled (long trip)."""
        schedule, _ = compute_schedule(SAMPLE_TARIFF, overnight_enabled=True)
        weekend = next(e for e in schedule if "sat" in e["days"])
        assert weekend["start"] == "00:00"
        assert weekend["end"] == "23:59"

    def test_weekend_days_are_sat_and_sun(self):
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        weekend = next(e for e in schedule if "sat" in e["days"])
        assert set(weekend["days"]) == {"sat", "sun"}

    def test_weekend_max_amps_is_32(self):
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        weekend = next(e for e in schedule if "sat" in e["days"])
        assert weekend["max_amps"] == 32

    def test_reasoning_contains_peak_hours(self):
        _, reasoning = compute_schedule(SAMPLE_TARIFF)
        assert "15" in reasoning
        assert "20" in reasoning

    def test_empty_tariff_falls_back_to_aps_defaults(self):
        """With {}, the APS default 16:00–19:00 fallback applies."""
        schedule, reasoning = compute_schedule({})
        weekday = next(e for e in schedule if "mon" in e["days"])
        assert weekday["start"] == "19:00"
        assert weekday["end"] == "16:00"

    def test_empty_tariff_reasoning_mentions_aps_default(self):
        _, reasoning = compute_schedule({})
        assert "APS default" in reasoning

    def test_schedule_entries_have_all_required_keys(self):
        required_keys = {"label", "days", "start", "end", "max_amps"}
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        for entry in schedule:
            assert required_keys.issubset(set(entry.keys())), (
                f"Entry missing keys: {required_keys - set(entry.keys())}"
            )


# ---------------------------------------------------------------------------
# Enphase app-api format fixtures (real tariff shape from Enlighten API)
# ---------------------------------------------------------------------------

# Winter weekday periods (minutes from midnight):
#   00:00-09:59  mid-peak   $0.049  (startTime=0,    endTime=599)
#   10:00-14:59  super off-peak $0.036  (catch-all — no explicit times)
#   15:00-15:59  mid-peak   $0.061  (startTime=900,  endTime=959)
#   16:00-18:59  peak       $0.101  (startTime=960,  endTime=1139)
#   19:00-23:59  mid-peak   $0.061  (startTime=1140, endTime=1439)
ENPHASE_WINTER_TARIFF = {
    "purchase": {
        "seasons": [
            {
                "id": "winter",
                "startMonth": "11",
                "endMonth": "4",
                "days": [
                    {
                        "id": "weekdays",
                        "days": [1, 2, 3, 4, 5],
                        "periods": [
                            {"id": "off-peak",  "startTime": "",    "endTime": "",    "rate": "0.03643", "type": "off-peak"},
                            {"id": "period-0",  "startTime": 0,     "endTime": 599,   "rate": "0.04854", "type": "mid-peak"},
                            {"id": "period-2",  "startTime": 900,   "endTime": 959,   "rate": "0.06086", "type": "mid-peak"},
                            {"id": "period-3",  "startTime": 960,   "endTime": 1139,  "rate": "0.10080", "type": "peak"},
                            {"id": "period-1",  "startTime": 1140,  "endTime": 1439,  "rate": "0.06086", "type": "mid-peak"},
                        ],
                    },
                    {
                        "id": "weekend",
                        "days": [6, 7],
                        "periods": [
                            {"id": "period-1",  "startTime": 0,     "endTime": 1439,  "rate": "0.06086", "type": "peak"},
                        ],
                    },
                ],
            },
        ]
    }
}

# Summer: only on-peak window (no super off-peak)
ENPHASE_SUMMER_TARIFF = {
    "purchase": {
        "seasons": [
            {
                "id": "summer",
                "startMonth": "5",
                "endMonth": "10",
                "days": [
                    {
                        "id": "weekdays",
                        "days": [1, 2, 3, 4, 5],
                        "periods": [
                            {"id": "off-peak",  "startTime": "",   "endTime": "",   "rate": "0.04849", "type": "off-peak"},
                            {"id": "period-0",  "startTime": 960,  "endTime": 1139, "rate": "0.14375", "type": "peak"},
                        ],
                    },
                ],
            },
        ]
    }
}


# ===========================================================================
# _find_daytime_window tests
# ===========================================================================

class TestFindDaytimeWindow:

    def test_winter_detects_super_off_peak_gap(self):
        peak = {"start_h": 16, "end_h": 19, "source": "test"}
        result = _find_daytime_window(ENPHASE_WINTER_TARIFF, peak)
        assert result["start"] == "10:00"
        assert result["end"]   == "15:00"

    def test_summer_falls_back_to_10_to_peak(self):
        peak = {"start_h": 16, "end_h": 19, "source": "test"}
        result = _find_daytime_window(ENPHASE_SUMMER_TARIFF, peak)
        assert result["start"] == "10:00"
        assert result["end"]   == "16:00"

    def test_empty_tariff_falls_back_to_peak_start(self):
        peak = {"start_h": 16, "end_h": 19, "source": "test"}
        result = _find_daytime_window({}, peak)
        assert result["start"] == "10:00"
        assert result["end"]   == "16:00"


# ===========================================================================
# compute_schedule — overnight_enabled flag
# ===========================================================================

class TestComputeScheduleOvernightFlag:

    # --- overnight_enabled=True (default) ---

    def test_overnight_enabled_weekday_wraps_overnight(self):
        """Full non-peak window: charges from peak_end back to peak_start."""
        schedule, _ = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=True)
        wd = next(e for e in schedule if "mon" in e["days"])
        assert wd["start"] == "19:00"
        assert wd["end"]   == "16:00"

    def test_overnight_enabled_reasoning_mentions_enabled(self):
        _, reasoning = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=True)
        assert "overnight charging enabled" in reasoning

    # --- overnight_enabled=False ---

    def test_overnight_disabled_winter_uses_super_off_peak(self):
        """No overnight draw — weekday window is super off-peak only."""
        schedule, _ = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=False)
        wd = next(e for e in schedule if "mon" in e["days"])
        assert wd["start"] == "10:00"
        assert wd["end"]   == "15:00"

    def test_overnight_disabled_summer_uses_daytime_to_peak(self):
        schedule, _ = compute_schedule(ENPHASE_SUMMER_TARIFF, overnight_enabled=False)
        wd = next(e for e in schedule if "mon" in e["days"])
        assert wd["start"] == "10:00"
        assert wd["end"]   == "16:00"

    def test_overnight_disabled_reasoning_mentions_disabled(self):
        _, reasoning = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=False)
        assert "overnight disabled" in reasoning

    def test_overnight_disabled_weekend_uses_daytime_window(self):
        """Weekend narrows to the same daytime window as weekdays when overnight is disabled."""
        sched_on,  _ = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=True)
        sched_off, _ = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=False)
        we_on  = next(e for e in sched_on  if "sat" in e["days"])
        we_off = next(e for e in sched_off if "sat" in e["days"])
        wd_off = next(e for e in sched_off if "mon" in e["days"])
        # overnight=True uses full-day window so charging starts immediately
        assert we_on["start"] == "00:00"
        assert we_on["end"]   == "23:59"
        # overnight=False: weekend matches the weekday daytime window
        assert we_off["start"] == wd_off["start"]
        assert we_off["end"]   == wd_off["end"]
        # and it must not span midnight
        assert int(we_off["start"].split(":")[0]) < int(we_off["end"].split(":")[0])

    def test_overnight_disabled_no_overnight_hours_in_window(self):
        """The daytime window must not span midnight (start < end)."""
        schedule, _ = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=False)
        wd = next(e for e in schedule if "mon" in e["days"])
        start_h = int(wd["start"].split(":")[0])
        end_h   = int(wd["end"].split(":")[0])
        assert start_h < end_h, "Daytime window should not wrap overnight"

    def test_overnight_disabled_schedule_has_required_keys(self):
        required = {"label", "days", "start", "end", "max_amps"}
        schedule, _ = compute_schedule(ENPHASE_WINTER_TARIFF, overnight_enabled=False)
        for entry in schedule:
            assert required.issubset(set(entry.keys()))
