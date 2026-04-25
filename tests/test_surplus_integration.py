"""
Integration tests for the surplus monitor state machine in server.py.

Verifies that _surplus_monitor_run correctly transitions between
tou_schedule and surplus_override modes and interacts with the JuiceBox MCP.
"""

import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock

import pytz
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import surplus_monitor

ARIZONA = pytz.timezone("America/Phoenix")

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_summary(soc: int, prod: int, cons: int) -> dict:
    # soc array: one completed interval (non-None) so last_idx=0; future slots use None
    return {
        "today_stats": {
            "battery_details": {"aggregate_soc": soc},
            "stats": [{
                "production":  [prod],
                "consumption": [cons],
                "solar_grid":  [0],
                "soc":         [soc],
            }],
        }
    }


@pytest.fixture(autouse=True)
def _reset_state():
    server._initialize_state()
    # Override _surplus_lock with a fresh lock each test so lock state doesn't leak
    import asyncio
    server._surplus_lock = asyncio.Lock()
    # Seed a default tariff so peak window is known (16:00–19:00)
    server._cached_tariff = {}
    yield


@pytest.fixture
def mock_juicebox(monkeypatch):
    mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("server.juicebox_mcp.set_charging_schedule", mock)
    return mock


@pytest.fixture
def daytime_non_peak(monkeypatch):
    """Freeze time to 10:00 Arizona (daylight, non-peak)."""
    fixed = datetime(2024, 6, 15, 10, 0, 0, tzinfo=ARIZONA)
    monkeypatch.setattr("server.datetime", type("dt", (), {
        "now": staticmethod(lambda tz=None: fixed),
        "strftime": fixed.strftime,
    }))


# ── Test: activate after ACTIVATION_POLLS consecutive surplus readings ────────

class TestActivation:

    async def test_no_activation_after_one_poll(self, mock_juicebox, monkeypatch):
        """Single surplus reading does not activate (requires ACTIVATION_POLLS=2)."""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=96, prod=3000, cons=1000)))

        await server._surplus_monitor_run()

        assert server._surplus_state["mode"] == "tou_schedule"
        mock_juicebox.assert_not_called()
        assert server._surplus_state["surplus_poll_count"] == 1

    async def test_activates_after_two_consecutive_polls(self, mock_juicebox, monkeypatch):
        """Two consecutive surplus readings activate surplus charging."""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=96, prod=3000, cons=1000)))

        await server._surplus_monitor_run()
        await server._surplus_monitor_run()

        assert server._surplus_state["mode"] == "surplus_override"
        mock_juicebox.assert_called_once()
        schedule = mock_juicebox.call_args[0][0]
        assert len(schedule) == 1
        assert schedule[0]["max_amps"] == surplus_monitor.compute_charge_amps(2000)

    async def test_activation_charge_amps_correct(self, mock_juicebox, monkeypatch):
        """Charge amps match surplus watts / 240V, clamped to [6, 32]."""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        # 1200W surplus → 1200//240 = 5A → clamped to 6A minimum
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=97, prod=2200, cons=1000)))

        await server._surplus_monitor_run()
        await server._surplus_monitor_run()

        schedule = mock_juicebox.call_args[0][0]
        assert schedule[0]["max_amps"] == 6  # clamped minimum


# ── Test: deactivate after DEACTIVATION_POLLS consecutive non-surplus readings ─

class TestDeactivation:

    async def _activate(self, mock_juicebox, monkeypatch):
        """Helper: run two surplus polls to enter surplus_override."""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=96, prod=3000, cons=1000)))
        await server._surplus_monitor_run()
        await server._surplus_monitor_run()
        assert server._surplus_state["mode"] == "surplus_override"
        mock_juicebox.reset_mock()

    async def test_no_deactivation_after_one_non_surplus_poll(self, mock_juicebox, monkeypatch):
        """Single non-surplus reading does not deactivate (requires DEACTIVATION_POLLS=2)."""
        await self._activate(mock_juicebox, monkeypatch)
        now = datetime(2024, 6, 15, 10, 15, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=80, prod=500, cons=1500)))

        await server._surplus_monitor_run()

        assert server._surplus_state["mode"] == "surplus_override"
        mock_juicebox.assert_not_called()

    async def test_deactivates_after_two_non_surplus_polls(self, mock_juicebox, monkeypatch):
        """Two consecutive non-surplus readings revert to TOU schedule."""
        await self._activate(mock_juicebox, monkeypatch)
        now = datetime(2024, 6, 15, 10, 15, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=80, prod=500, cons=1500)))
        # Seed _last_result so revert has a TOU schedule to restore
        server._last_result = {"schedule": [{"label": "TOU"}]}

        await server._surplus_monitor_run()
        await server._surplus_monitor_run()

        assert server._surplus_state["mode"] == "tou_schedule"
        mock_juicebox.assert_called_once_with([{"label": "TOU"}])


# ── Test: peak window guard ────────────────────────────────────────────────────

class TestPeakWindowGuard:

    async def test_no_activation_during_peak_window(self, mock_juicebox, monkeypatch):
        """Surplus is ignored during the peak window (16:00–19:00 + buffer)."""
        # 16:30 — inside peak window
        now = datetime(2024, 6, 15, 16, 30, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=97, prod=4000, cons=1000)))

        await server._surplus_monitor_run()
        await server._surplus_monitor_run()

        assert server._surplus_state["mode"] == "tou_schedule"
        mock_juicebox.assert_not_called()

    async def test_surplus_override_reverted_when_peak_starts(self, mock_juicebox, monkeypatch):
        """If surplus override is active and peak starts, it is immediately reverted."""
        # First activate during non-peak
        now_before = datetime(2024, 6, 15, 15, 0, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now_before)}))
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary",
                            AsyncMock(return_value=_make_summary(soc=96, prod=3000, cons=1000)))
        await server._surplus_monitor_run()
        await server._surplus_monitor_run()
        assert server._surplus_state["mode"] == "surplus_override"
        mock_juicebox.reset_mock()
        server._last_result = {"schedule": []}

        # Now poll at 16:30 (inside peak)
        now_peak = datetime(2024, 6, 15, 16, 30, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now_peak)}))

        await server._surplus_monitor_run()

        assert server._surplus_state["mode"] == "tou_schedule"
        mock_juicebox.assert_called_once_with([])


# ── Test: no-op outside daylight hours ────────────────────────────────────────

class TestOutsideDaylightHours:

    async def test_does_nothing_before_6am(self, mock_juicebox, monkeypatch):
        now = datetime(2024, 6, 15, 5, 30, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        get_summary = AsyncMock()
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary", get_summary)

        await server._surplus_monitor_run()

        get_summary.assert_not_called()
        mock_juicebox.assert_not_called()

    async def test_does_nothing_at_8pm(self, mock_juicebox, monkeypatch):
        now = datetime(2024, 6, 15, 20, 0, tzinfo=ARIZONA)
        monkeypatch.setattr("server.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now)}))
        get_summary = AsyncMock()
        monkeypatch.setattr("server.enphase_mcp.get_energy_summary", get_summary)

        await server._surplus_monitor_run()

        get_summary.assert_not_called()
        mock_juicebox.assert_not_called()
