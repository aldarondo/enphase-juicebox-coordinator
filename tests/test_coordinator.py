"""
Tests for coordinator.py — mocks enphase_mcp, optimizer, and juicebox_mcp.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coordinator  # noqa: E402 — imported after sys.path is set


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

SAMPLE_TARIFF = {
    "purchase": {
        "typeId": "tou",
        "seasons": [
            {
                "id": "summer",
                "startMonth": "1",
                "endMonth": "12",
                "days": [
                    {
                        "id": "weekdays",
                        "days": [1, 2, 3, 4, 5],
                        "periods": [
                            {"id": "off-peak", "startTime": "", "endTime": "", "rate": "0.05", "type": "off-peak"},
                            {"id": "on-peak",  "startTime": 960, "endTime": 1139, "rate": "0.14", "type": "peak"},
                        ],
                    },
                    {
                        "id": "weekend",
                        "days": [6, 7],
                        "periods": [
                            {"id": "period-1", "startTime": 0, "endTime": 1439, "rate": "0.06", "type": "peak"},
                        ],
                    },
                ],
            }
        ],
    }
}

SAMPLE_SCHEDULE = [
    {
        "label": "test window",
        "days": ["mon"],
        "start": "20:00",
        "end": "15:00",
        "max_amps": 32,
    }
]
SAMPLE_REASONING = "Peak 15:00-20:00"


@pytest.fixture
def mocks(monkeypatch):
    monkeypatch.setattr(
        "coordinator.enphase_mcp.get_tariff",
        AsyncMock(return_value=SAMPLE_TARIFF),
    )
    monkeypatch.setattr(
        "coordinator.optimizer.compute_schedule",
        lambda t: (SAMPLE_SCHEDULE, SAMPLE_REASONING),
    )
    monkeypatch.setattr(
        "coordinator.juicebox_mcp.set_charging_schedule",
        AsyncMock(return_value={"success": True}),
    )

    return {
        "schedule": SAMPLE_SCHEDULE,
        "reasoning": SAMPLE_REASONING,
    }


# ===========================================================================
# Happy path
# ===========================================================================

class TestCoordinatorHappyPath:

    async def test_returns_status_ok(self, mocks):
        result = await coordinator.run()
        assert result["status"] == "ok"

    async def test_juicebox_ok_true(self, mocks):
        result = await coordinator.run()
        assert result["juicebox_ok"] is True

    async def test_errors_empty(self, mocks):
        result = await coordinator.run()
        assert result["errors"] == []

    async def test_schedule_matches(self, mocks):
        result = await coordinator.run()
        assert result["schedule"] == mocks["schedule"]

    async def test_reasoning_matches(self, mocks):
        result = await coordinator.run()
        assert result["reasoning"] == mocks["reasoning"]

    async def test_started_at_set(self, mocks):
        result = await coordinator.run()
        assert result.get("started_at") is not None

    async def test_finished_at_set(self, mocks):
        result = await coordinator.run()
        assert result.get("finished_at") is not None


# ===========================================================================
# Tariff fetch fails → partial (juicebox still succeeds)
# ===========================================================================

class TestTariffFetchFails:

    @pytest.fixture
    def mocks_tariff_fails(self, mocks, monkeypatch):
        monkeypatch.setattr(
            "coordinator.enphase_mcp.get_tariff",
            AsyncMock(side_effect=Exception("network error")),
        )
        return mocks

    async def test_errors_contains_tariff_message(self, mocks_tariff_fails):
        result = await coordinator.run()
        assert any("network error" in e for e in result["errors"])

    async def test_status_is_partial_when_juicebox_ok(self, mocks_tariff_fails):
        """errors=[tariff_err], juicebox_ok=True -> status="partial"."""
        result = await coordinator.run()
        assert result["status"] == "partial"

    async def test_juicebox_ok_still_true(self, mocks_tariff_fails):
        result = await coordinator.run()
        assert result["juicebox_ok"] is True


# ===========================================================================
# JuiceBox push fails → error
# ===========================================================================

class TestJuiceboxFails:

    @pytest.fixture
    def mocks_jb_fails(self, mocks, monkeypatch):
        monkeypatch.setattr(
            "coordinator.juicebox_mcp.set_charging_schedule",
            AsyncMock(side_effect=Exception("connection refused")),
        )
        return mocks

    async def test_errors_contains_juicebox_message(self, mocks_jb_fails):
        result = await coordinator.run()
        assert any("connection refused" in e for e in result["errors"])

    async def test_juicebox_ok_false(self, mocks_jb_fails):
        result = await coordinator.run()
        assert result["juicebox_ok"] is False

    async def test_status_is_error(self, mocks_jb_fails):
        result = await coordinator.run()
        assert result["status"] == "error"


# ===========================================================================
# Both tariff and juicebox fail → error with 2 entries
# ===========================================================================

class TestBothFail:

    @pytest.fixture
    def mocks_both_fail(self, mocks, monkeypatch):
        monkeypatch.setattr(
            "coordinator.enphase_mcp.get_tariff",
            AsyncMock(side_effect=Exception("tariff gone")),
        )
        monkeypatch.setattr(
            "coordinator.juicebox_mcp.set_charging_schedule",
            AsyncMock(side_effect=Exception("juicebox gone")),
        )
        return mocks

    async def test_status_is_error(self, mocks_both_fail):
        result = await coordinator.run()
        assert result["status"] == "error"

    async def test_errors_has_two_entries(self, mocks_both_fail):
        result = await coordinator.run()
        assert len(result["errors"]) == 2

    async def test_juicebox_ok_false(self, mocks_both_fail):
        result = await coordinator.run()
        assert result["juicebox_ok"] is False
