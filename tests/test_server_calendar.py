"""
Tests for server._nightly_calendar_check — verifies that the 21:00 check
pushes its decision to the JuiceBox immediately (not deferred to 04:00).
"""

import os
import sys

import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset shared state between tests so assertions aren't cross-contaminated."""
    server._overnight_charging = {
        "enabled":         False,
        "reason":          "test-default",
        "set_at":          None,
        "calendar_result": None,
    }
    server._last_result = None
    yield


@pytest.fixture(autouse=True)
def _ical_urls(monkeypatch):
    monkeypatch.setenv("GOOGLE_ICAL_URLS", "https://example.com/cal.ics")


# ===========================================================================
# Long trip tomorrow → coordinator.run() called immediately (pushes TOU schedule)
# ===========================================================================

class TestLongTripEnablesAndPushes:

    @pytest.fixture
    def mocks(self, monkeypatch):
        check_mock = AsyncMock(return_value={
            "overnight_charging_needed": True,
            "reasoning": "Tomorrow's trip is 120 miles",
        })
        run_mock = AsyncMock(return_value={
            "status":      "ok",
            "schedule":    [{"label": "weekday TOU"}],
            "juicebox_ok": True,
        })
        clear_mock = AsyncMock()
        monkeypatch.setattr(
            "server.calendar_check.check_tomorrow_driving", check_mock,
        )
        monkeypatch.setattr("server.coordinator.run", run_mock)
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule", clear_mock,
        )
        return {"check": check_mock, "run": run_mock, "clear": clear_mock}

    async def test_coordinator_run_called(self, mocks):
        await server._nightly_calendar_check()
        mocks["run"].assert_called_once()

    async def test_clear_not_called(self, mocks):
        await server._nightly_calendar_check()
        mocks["clear"].assert_not_called()

    async def test_flag_enabled(self, mocks):
        await server._nightly_calendar_check()
        assert server._overnight_charging["enabled"] is True

    async def test_last_result_updated(self, mocks):
        await server._nightly_calendar_check()
        assert server._last_result is not None
        assert server._last_result["status"] == "ok"


# ===========================================================================
# No trip tomorrow → JuiceBox schedule cleared immediately
# ===========================================================================

class TestNoTripClearsSchedule:

    @pytest.fixture
    def mocks(self, monkeypatch):
        check_mock = AsyncMock(return_value={
            "overnight_charging_needed": False,
            "reasoning": "No events tomorrow",
        })
        run_mock = AsyncMock(return_value={"status": "ok"})
        clear_mock = AsyncMock(return_value={"success": True})
        # get_tariff is now called to compute the daytime-only schedule
        tariff_mock = AsyncMock(return_value={})
        monkeypatch.setattr(
            "server.calendar_check.check_tomorrow_driving", check_mock,
        )
        monkeypatch.setattr("server.coordinator.run", run_mock)
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule", clear_mock,
        )
        monkeypatch.setattr("server.enphase_mcp.get_tariff", tariff_mock)
        return {"check": check_mock, "run": run_mock, "clear": clear_mock, "tariff": tariff_mock}

    async def test_daytime_schedule_pushed(self, mocks):
        # No trip → daytime-only window pushed (not empty []); JuiceBox defaults
        # to charging freely when given [], so we push a real restricting schedule.
        await server._nightly_calendar_check()
        mocks["clear"].assert_called_once()
        pushed = mocks["clear"].call_args[0][0]
        assert isinstance(pushed, list) and len(pushed) > 0, "expected a non-empty daytime schedule"
        weekday = next(w for w in pushed if "mon" in w.get("days", []))
        assert weekday["start"] != "19:00", "overnight wrap-around schedule must not be pushed"

    async def test_coordinator_run_not_called(self, mocks):
        await server._nightly_calendar_check()
        mocks["run"].assert_not_called()

    async def test_flag_disabled(self, mocks):
        await server._nightly_calendar_check()
        assert server._overnight_charging["enabled"] is False

    async def test_last_result_reflects_daytime_schedule(self, mocks):
        await server._nightly_calendar_check()
        assert isinstance(server._last_result["schedule"], list)
        assert len(server._last_result["schedule"]) > 0
        assert server._last_result["status"] == "ok"


# ===========================================================================
# JuiceBox push fails → flag still set, log warns, no crash (safety-net handles)
# ===========================================================================

class TestJuiceboxPushFails:

    @pytest.fixture
    def mocks(self, monkeypatch):
        check_mock = AsyncMock(return_value={
            "overnight_charging_needed": True,
            "reasoning": "Long trip",
        })
        run_mock = AsyncMock(side_effect=Exception("juicebox unreachable"))
        monkeypatch.setattr(
            "server.calendar_check.check_tomorrow_driving", check_mock,
        )
        monkeypatch.setattr("server.coordinator.run", run_mock)
        return {"check": check_mock, "run": run_mock}

    async def test_does_not_raise(self, mocks):
        # Should swallow exception — 04:00 safety-net retries
        await server._nightly_calendar_check()

    async def test_flag_still_set(self, mocks):
        await server._nightly_calendar_check()
        assert server._overnight_charging["enabled"] is True


# ===========================================================================
# Calendar check itself fails → flag untouched, no JuiceBox call
# ===========================================================================

class TestCalendarCheckFails:

    @pytest.fixture
    def mocks(self, monkeypatch):
        check_mock = AsyncMock(side_effect=Exception("ical fetch failed"))
        run_mock = AsyncMock()
        clear_mock = AsyncMock()
        monkeypatch.setattr(
            "server.calendar_check.check_tomorrow_driving", check_mock,
        )
        monkeypatch.setattr("server.coordinator.run", run_mock)
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule", clear_mock,
        )
        return {"run": run_mock, "clear": clear_mock}

    async def test_no_juicebox_calls(self, mocks):
        await server._nightly_calendar_check()
        mocks["run"].assert_not_called()
        mocks["clear"].assert_not_called()

    async def test_flag_unchanged(self, mocks):
        await server._nightly_calendar_check()
        # Fixture's default state has enabled=False; check still False
        assert server._overnight_charging["enabled"] is False


# ===========================================================================
# No iCal URLs → early return, no calls
# ===========================================================================

class TestNoIcalUrls:

    async def test_early_return_no_calls(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_ICAL_URLS", "")
        check_mock = AsyncMock()
        run_mock = AsyncMock()
        clear_mock = AsyncMock()
        monkeypatch.setattr(
            "server.calendar_check.check_tomorrow_driving", check_mock,
        )
        monkeypatch.setattr("server.coordinator.run", run_mock)
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule", clear_mock,
        )

        await server._nightly_calendar_check()

        check_mock.assert_not_called()
        run_mock.assert_not_called()
        clear_mock.assert_not_called()
