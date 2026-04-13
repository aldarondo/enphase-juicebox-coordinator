"""
Tests for optimizer.py — pure functions, no I/O, no mocking needed.
"""

import sys
import os
import pytest
from datetime import date

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer import _find_peak_weekday_hours, compute_schedule

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

    def test_weekend_window_is_fixed(self):
        """Weekend window is always 08:00–22:00."""
        schedule, _ = compute_schedule(SAMPLE_TARIFF)
        weekend = next(e for e in schedule if "sat" in e["days"])
        assert weekend["start"] == "08:00"
        assert weekend["end"] == "22:00"

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
        """With {}, the APS default 15:00–20:00 fallback applies."""
        schedule, reasoning = compute_schedule({})
        weekday = next(e for e in schedule if "mon" in e["days"])
        assert weekday["start"] == "20:00"
        assert weekday["end"] == "15:00"

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
