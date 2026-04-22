"""
Tests for the `set_overnight_mode` MCP tool handler.

Verifies consistency with `_nightly_calendar_check`: manually flipping the
overnight flag should immediately push the resulting schedule to the JuiceBox
(TOU when enabled, empty when disabled) — not wait for the 04:00 run.
"""

import json
import os
import sys

import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    server._overnight_charging = {
        "enabled":         False,
        "reason":          "test-default",
        "set_at":          None,
        "calendar_result": None,
    }
    server._last_result = None
    yield


async def _invoke(name: str, arguments: dict) -> dict:
    """Call the registered MCP tool handler and return the parsed JSON payload."""
    handlers = server.app.request_handlers
    from mcp.types import CallToolRequest, CallToolRequestParams
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    handler = handlers[CallToolRequest]
    result = await handler(req)
    # ServerResult wraps CallToolResult; payload is first TextContent
    content = result.root.content
    return json.loads(content[0].text)


# ===========================================================================
# enable=true  → coordinator.run() called, flag enabled
# ===========================================================================

class TestEnableTrue:

    @pytest.fixture
    def mocks(self, monkeypatch):
        run_mock = AsyncMock(return_value={
            "status":      "ok",
            "schedule":    [{"label": "weekday TOU"}],
            "juicebox_ok": True,
        })
        clear_mock = AsyncMock()
        monkeypatch.setattr("server.coordinator.run", run_mock)
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule", clear_mock,
        )
        return {"run": run_mock, "clear": clear_mock}

    async def test_coordinator_run_called(self, mocks):
        await _invoke("set_overnight_mode", {"enable": True, "reason": "big trip"})
        mocks["run"].assert_called_once()

    async def test_clear_not_called(self, mocks):
        await _invoke("set_overnight_mode", {"enable": True, "reason": "big trip"})
        mocks["clear"].assert_not_called()

    async def test_flag_enabled(self, mocks):
        await _invoke("set_overnight_mode", {"enable": True, "reason": "big trip"})
        assert server._overnight_charging["enabled"] is True

    async def test_response_status_ok(self, mocks):
        payload = await _invoke("set_overnight_mode", {"enable": True, "reason": "big trip"})
        assert payload["status"] == "ok"

    async def test_response_juicebox_ok(self, mocks):
        payload = await _invoke("set_overnight_mode", {"enable": True, "reason": "big trip"})
        assert payload["juicebox_ok"] is True


# ===========================================================================
# enable=false  → JuiceBox schedule cleared, flag disabled
# ===========================================================================

class TestEnableFalse:

    @pytest.fixture
    def mocks(self, monkeypatch):
        run_mock = AsyncMock()
        clear_mock = AsyncMock(return_value={"success": True})
        monkeypatch.setattr("server.coordinator.run", run_mock)
        monkeypatch.setattr(
            "server.juicebox_mcp.set_charging_schedule", clear_mock,
        )
        return {"run": run_mock, "clear": clear_mock}

    async def test_clear_called_with_empty_list(self, mocks):
        await _invoke("set_overnight_mode", {"enable": False, "reason": "surplus only"})
        mocks["clear"].assert_called_once_with([])

    async def test_run_not_called(self, mocks):
        await _invoke("set_overnight_mode", {"enable": False, "reason": "surplus only"})
        mocks["run"].assert_not_called()

    async def test_flag_disabled(self, mocks):
        await _invoke("set_overnight_mode", {"enable": False, "reason": "surplus only"})
        assert server._overnight_charging["enabled"] is False

    async def test_last_result_cleared(self, mocks):
        await _invoke("set_overnight_mode", {"enable": False, "reason": "surplus only"})
        assert server._last_result["schedule"] == []


# ===========================================================================
# JuiceBox push fails → status=push_failed, flag still set
# ===========================================================================

class TestPushFails:

    @pytest.fixture
    def mocks(self, monkeypatch):
        run_mock = AsyncMock(side_effect=Exception("juicebox unreachable"))
        monkeypatch.setattr("server.coordinator.run", run_mock)
        return {"run": run_mock}

    async def test_status_push_failed(self, mocks):
        payload = await _invoke("set_overnight_mode", {"enable": True, "reason": "x"})
        assert payload["status"] == "push_failed"

    async def test_flag_still_set(self, mocks):
        await _invoke("set_overnight_mode", {"enable": True, "reason": "x"})
        # Flag is set before the push attempt, so it stays set even on push failure
        assert server._overnight_charging["enabled"] is True

    async def test_juicebox_ok_false(self, mocks):
        payload = await _invoke("set_overnight_mode", {"enable": True, "reason": "x"})
        assert payload["juicebox_ok"] is False
