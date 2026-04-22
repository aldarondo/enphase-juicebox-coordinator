"""
Tests for server._peak_switch_times — verifies battery-mode switch times are
derived from the tariff peak window (with APS default fallback).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def _tariff_with_peak(start_min: int, end_min: int) -> dict:
    """Build a minimal tariff with a single weekday peak period."""
    return {
        "purchase": {
            "seasons": [{
                "id": "year",
                "startMonth": "1",
                "endMonth":   "12",
                "days": [{
                    "id":   "weekdays",
                    "days": [1, 2, 3, 4, 5],
                    "periods": [
                        {"id": "off-peak", "startTime": "", "endTime": "", "rate": "0.05", "type": "off-peak"},
                        {"id": "on-peak",  "startTime": start_min, "endTime": end_min, "rate": "0.14", "type": "peak"},
                    ],
                }],
            }],
        }
    }


class TestDefaultAPS:
    """Empty tariff → APS default 16:00–19:00 → switches at 15:57 and 19:02."""

    def test_source_is_default(self):
        t = server._peak_switch_times({})
        assert t["source"] == "default"

    def test_pre_peak_1557(self):
        t = server._peak_switch_times({})
        assert (t["pre_h"], t["pre_m"]) == (15, 57)

    def test_post_peak_1902(self):
        t = server._peak_switch_times({})
        assert (t["post_h"], t["post_m"]) == (19, 2)


class TestTariffDerived:
    """Tariff defines 16:00–19:00 → same 15:57 / 19:02, source=tariff."""

    def test_source_is_tariff(self):
        t = server._peak_switch_times(_tariff_with_peak(960, 1139))  # 16:00–18:59
        assert t["source"] == "tariff"

    def test_pre_peak_from_tariff(self):
        t = server._peak_switch_times(_tariff_with_peak(960, 1139))
        assert (t["pre_h"], t["pre_m"]) == (15, 57)

    def test_post_peak_from_tariff(self):
        t = server._peak_switch_times(_tariff_with_peak(960, 1139))
        assert (t["post_h"], t["post_m"]) == (19, 2)


class TestShiftedPeakWindow:
    """If tariff changes peak to 15:00–18:00, switches must shift to 14:57 / 18:02."""

    def test_pre_peak_shifts(self):
        t = server._peak_switch_times(_tariff_with_peak(900, 1079))  # 15:00–17:59
        assert (t["pre_h"], t["pre_m"]) == (14, 57)

    def test_post_peak_shifts(self):
        t = server._peak_switch_times(_tariff_with_peak(900, 1079))
        assert (t["post_h"], t["post_m"]) == (18, 2)


class TestSchedulerRegistration:
    """Verify the registered cron jobs are weekday-only."""

    def test_both_mode_jobs_are_weekday_only(self):
        scheduler = server._build_scheduler()
        for job_id in ("battery_mode_pre_peak", "battery_mode_post_peak"):
            job = scheduler.get_job(job_id)
            # CronTrigger stores fields as list of field objects; stringify and check
            trigger_str = str(job.trigger)
            assert "day_of_week='mon-fri'" in trigger_str, \
                f"{job_id} cron should be weekday-only, got: {trigger_str}"
