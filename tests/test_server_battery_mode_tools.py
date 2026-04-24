"""
Tests for the battery-mode MCP tool handlers in server.py:
  - switch_battery_mode  (manual override)
  - get_battery_mode_status

Exercises the real MCP CallToolRequest handler path so the tool dispatch,
argument validation, and shared-state updates are all covered.
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
    server._last_mode_switch = None
    yield


async def _invoke(name: str, arguments: dict) -> dict:
    """Call the registered MCP tool handler and return the parsed JSON payload."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    handler = server.app.request_handlers[CallToolRequest]
    result = await handler(req)
    return json.loads(result.root.content[0].text)


async def _invoke_raw(name: str, arguments: dict):
    """Return the raw CallToolResult (used to inspect isError for schema validation)."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    handler = server.app.request_handlers[CallToolRequest]
    return (await handler(req)).root


# ===========================================================================
# switch_battery_mode — happy path + invalid-mode validation
# ===========================================================================

class TestSwitchBatteryMode:

    @pytest.fixture
    def switch_mock(self, monkeypatch):
        mock = AsyncMock(return_value={
            "status":       "ok",
            "target_mode":  "self-consumption",
            "applied_mode": "self-consumption",
            "label":        "manual",
            "attempts":     1,
        })
        monkeypatch.setattr("server.battery_mode.switch_to", mock)
        return mock

    async def test_self_consumption_calls_switch_to(self, switch_mock):
        payload = await _invoke("switch_battery_mode", {"mode": "self-consumption"})
        switch_mock.assert_called_once()
        assert payload["status"] == "ok"

    async def test_cost_savings_calls_switch_to(self, monkeypatch):
        mock = AsyncMock(return_value={
            "status":       "ok",
            "target_mode":  "cost_savings",
            "applied_mode": "cost_savings",
            "label":        "manual",
            "attempts":     1,
        })
        monkeypatch.setattr("server.battery_mode.switch_to", mock)

        payload = await _invoke("switch_battery_mode", {"mode": "cost_savings"})

        mock.assert_called_once()
        assert payload["target_mode"] == "cost_savings"

    async def test_invalid_mode_rejected_by_schema_before_handler(self, monkeypatch):
        """MCP framework validates the enum from Tool.inputSchema before the
        handler runs, so switch_to is never called and isError=True."""
        mock = AsyncMock()
        monkeypatch.setattr("server.battery_mode.switch_to", mock)

        result = await _invoke_raw("switch_battery_mode", {"mode": "backup-only"})

        mock.assert_not_called()
        assert result.isError is True
        assert "not one of" in result.content[0].text

    async def test_last_mode_switch_updated(self, switch_mock):
        await _invoke("switch_battery_mode", {"mode": "self-consumption"})
        assert server._last_mode_switch is not None
        assert server._last_mode_switch["status"] == "ok"


# ===========================================================================
# get_battery_mode_status — never_run + populated branches
# ===========================================================================

class TestGetBatteryModeStatus:

    async def test_never_run_branch(self):
        server._last_mode_switch = None
        payload = await _invoke("get_battery_mode_status", {})
        assert payload["status"] == "never_run"
        assert "15:57" in payload["message"] and "19:02" in payload["message"]

    async def test_returns_last_switch_result(self):
        server._last_mode_switch = {
            "status":       "ok",
            "label":        "15:57 pre-peak",
            "target_mode":  "self-consumption",
            "applied_mode": "self-consumption",
            "attempts":     1,
            "errors":       [],
        }
        payload = await _invoke("get_battery_mode_status", {})
        assert payload["status"] == "ok"
        assert payload["label"] == "15:57 pre-peak"
        assert payload["applied_mode"] == "self-consumption"

    async def test_returns_error_result_from_failed_switch(self):
        server._last_mode_switch = {
            "status":       "error",
            "label":        "15:57 pre-peak",
            "target_mode":  "self-consumption",
            "attempts":     2,
            "errors":       ["attempt 1: network", "attempt 2: network"],
        }
        payload = await _invoke("get_battery_mode_status", {})
        assert payload["status"] == "error"
        assert payload["attempts"] == 2
        assert len(payload["errors"]) == 2
