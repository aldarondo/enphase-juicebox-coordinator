"""
Tests for server.py — charge_now and get_weekly_report tools,
plus the schedule verification helper.
"""

import sys
import os
import json
import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

ARIZONA = pytz.timezone("America/Phoenix")

SAMPLE_SCHEDULE = [
    {"label": "Weekday off-peak — avoid 16:00–19:00", "days": ["mon","tue","wed","thu","fri"],
     "start": "19:00", "end": "16:00", "max_amps": 32},
    {"label": "Weekend — no peak pricing", "days": ["sat","sun"],
     "start": "08:00", "end": "22:00", "max_amps": 32},
]
SAMPLE_TARIFF = {
    "purchase": {
        "seasons": [{
            "id": "all", "startMonth": "1", "endMonth": "12",
            "days": [{
                "id": "weekdays", "days": [1,2,3,4,5],
                "periods": [
                    {"id": "on-peak", "startTime": 960, "endTime": 1139,
                     "rate": "0.14", "type": "peak"},
                ],
            }],
        }],
    }
}


# ===========================================================================
# charge_now
# ===========================================================================

class TestChargeNow:

    @pytest.fixture(autouse=True)
    def mock_juicebox(self, monkeypatch):
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule",
            AsyncMock(return_value={"success": True}),
        )

    @pytest.fixture
    def frozen_now(self, monkeypatch):
        """Freeze Arizona time to a Wednesday at 14:30."""
        fake_now = ARIZONA.localize(datetime(2025, 7, 9, 14, 30, 0))  # Wednesday
        monkeypatch.setattr(
            "server.datetime",
            type("FakeDT", (), {
                "now": staticmethod(lambda tz=None: fake_now),
                "fromisoformat": datetime.fromisoformat,
            })(),
        )
        return fake_now

    async def test_charge_now_returns_ok(self, frozen_now):
        result = await server.call_tool("charge_now", {})
        payload = json.loads(result[0].text)
        assert payload["status"] == "ok"
        assert payload["override_active"] is True

    async def test_charge_now_default_end_is_2359(self, frozen_now):
        result = await server.call_tool("charge_now", {})
        payload = json.loads(result[0].text)
        assert payload["window"]["end"] == "23:59"

    async def test_charge_now_with_hours(self, frozen_now):
        result = await server.call_tool("charge_now", {"hours": 2})
        payload = json.loads(result[0].text)
        assert payload["window"]["start"] == "14:30"
        assert payload["window"]["end"] == "16:30"

    async def test_charge_now_caps_at_end_of_day(self, frozen_now):
        result = await server.call_tool("charge_now", {"hours": 12})
        payload = json.loads(result[0].text)
        assert payload["window"]["end"] == "23:59"

    async def test_charge_now_day_is_wednesday(self, frozen_now):
        result = await server.call_tool("charge_now", {})
        payload = json.loads(result[0].text)
        assert payload["window"]["days"] == ["wed"]

    async def test_charge_now_juicebox_error(self, monkeypatch):
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule",
            AsyncMock(side_effect=Exception("connection refused")),
        )
        fake_now = ARIZONA.localize(datetime(2025, 7, 9, 14, 30, 0))
        monkeypatch.setattr(
            "server.datetime",
            type("FakeDT", (), {
                "now": staticmethod(lambda tz=None: fake_now),
                "fromisoformat": datetime.fromisoformat,
            })(),
        )
        result = await server.call_tool("charge_now", {})
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert "connection refused" in payload["error"]

    async def test_note_mentions_resumption(self, frozen_now):
        result = await server.call_tool("charge_now", {})
        payload = json.loads(result[0].text)
        assert "04:00" in payload["note"]


# ===========================================================================
# get_weekly_report
# ===========================================================================

class TestGetWeeklyReport:

    async def test_no_report_returns_status_no_report(self, monkeypatch):
        monkeypatch.setattr("server._last_report", None)
        result = await server.call_tool("get_weekly_report", {})
        payload = json.loads(result[0].text)
        assert payload["status"] == "no_report"

    async def test_returns_stored_report(self, monkeypatch):
        fake_report = {"generated_at": "2025-07-06T06:00:00", "week_ending": "2025-07-06"}
        monkeypatch.setattr("server._last_report", fake_report)
        result = await server.call_tool("get_weekly_report", {})
        payload = json.loads(result[0].text)
        assert payload["week_ending"] == "2025-07-06"


# ===========================================================================
# _verify_schedule_against_tariff
# ===========================================================================

class TestVerifySchedule:

    @pytest.fixture(autouse=True)
    def reset_last_result(self, monkeypatch):
        monkeypatch.setattr("server._last_result", {
            "schedule": SAMPLE_SCHEDULE,
            "status": "ok",
        })

    @pytest.fixture(autouse=True)
    def mock_tariff(self, monkeypatch):
        monkeypatch.setattr(
            "server.enphase_mcp.get_tariff",
            AsyncMock(return_value=SAMPLE_TARIFF),
        )

    async def test_in_sync_when_schedules_match(self):
        result = await server._verify_schedule_against_tariff()
        assert result["status"] == "in_sync"

    async def test_drift_detected_when_schedules_differ(self, monkeypatch):
        stale_schedule = [
            {"label": "old", "days": ["mon","tue","wed","thu","fri"],
             "start": "20:00", "end": "15:00", "max_amps": 32},
        ]
        monkeypatch.setattr("server._last_result", {"schedule": stale_schedule, "status": "ok"})
        result = await server._verify_schedule_against_tariff()
        assert result["status"] == "drift_detected"
        assert "MISMATCH" in result["message"]

    async def test_error_when_tariff_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(
            "server.enphase_mcp.get_tariff",
            AsyncMock(side_effect=Exception("network error")),
        )
        result = await server._verify_schedule_against_tariff()
        assert result["status"] == "error"
        assert "network error" in result["error"]

    async def test_no_programmed_schedule(self, monkeypatch):
        monkeypatch.setattr("server._last_result", None)
        result = await server._verify_schedule_against_tariff()
        assert result["status"] == "no_programmed_schedule"
