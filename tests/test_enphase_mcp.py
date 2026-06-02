"""
Tests for enphase_mcp.py — focuses on error-payload detection.

The claude-enphase MCP server can signal failure three ways: an MCP-level
``isError``/empty result, a plain-text ``"Error: ..."`` body, or a structured
JSON error dict (e.g. ``{"error": "token expired"}``). All three must raise so
the real cause surfaces in retry logs and battery-mode failure alerts, instead
of flowing back as a value that ``_extract_mode`` turns into a misleading None.
"""

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import enphase_mcp  # noqa: E402


def make_mock_session(response_text: str | None = "{}", empty_content: bool = False,
                      is_error: bool = False):
    result = MagicMock()
    result.isError = is_error
    if empty_content:
        result.content = []
    else:
        content_item = MagicMock()
        content_item.text = response_text
        result.content = [content_item]

    session = AsyncMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def make_sse_context():
    read_mock = AsyncMock()
    write_mock = AsyncMock()
    sse_cm = MagicMock()
    sse_cm.__aenter__ = AsyncMock(return_value=(read_mock, write_mock))
    sse_cm.__aexit__ = AsyncMock(return_value=False)
    return sse_cm


def _patched(session):
    return (
        patch("enphase_mcp.sse_client", return_value=make_sse_context()),
        patch("enphase_mcp.ClientSession", return_value=session),
    )


async def _call(session, fn, *args):
    p1, p2 = _patched(session)
    with p1, p2:
        return await fn(*args)


# ===========================================================================
# _raise_if_error_payload unit tests
# ===========================================================================

class TestRaiseIfErrorPayload:

    def test_error_key_string_raises_with_detail(self):
        with pytest.raises(RuntimeError, match="token expired"):
            enphase_mcp._raise_if_error_payload({"error": "token expired"}, "t")

    def test_status_error_raises(self):
        with pytest.raises(RuntimeError, match="boom"):
            enphase_mcp._raise_if_error_payload(
                {"status": "error", "message": "boom"}, "t"
            )

    def test_status_failed_raises(self):
        with pytest.raises(RuntimeError):
            enphase_mcp._raise_if_error_payload({"status": "failed"}, "t")

    def test_status_ok_does_not_raise(self):
        enphase_mcp._raise_if_error_payload({"status": "ok", "usage": "self-consumption"}, "t")

    def test_error_falsy_does_not_raise(self):
        # error present but null/empty is not a failure
        enphase_mcp._raise_if_error_payload({"error": None, "usage": "cost_savings"}, "t")
        enphase_mcp._raise_if_error_payload({"error": ""}, "t")

    def test_non_dict_does_not_raise(self):
        enphase_mcp._raise_if_error_payload("self-consumption", "t")
        enphase_mcp._raise_if_error_payload(42, "t")

    def test_nested_error_dict_serialized_when_no_message(self):
        with pytest.raises(RuntimeError, match="DNS"):
            enphase_mcp._raise_if_error_payload({"error": {"code": "DNS"}}, "t")


# ===========================================================================
# get_battery_mode / set_battery_mode error propagation
# ===========================================================================

class TestGetBatteryMode:

    async def test_returns_parsed_dict_on_success(self):
        session = make_mock_session('{"usage": "self-consumption"}')
        result = await _call(session, enphase_mcp.get_battery_mode)
        assert result == {"usage": "self-consumption"}

    async def test_raises_on_error_dict(self):
        session = make_mock_session('{"error": "DNS failure resolving enphaseenergy.com"}')
        with pytest.raises(RuntimeError, match="DNS failure"):
            await _call(session, enphase_mcp.get_battery_mode)

    async def test_raises_on_error_text_prefix(self):
        session = make_mock_session("Error: upstream 503")
        with pytest.raises(RuntimeError, match="upstream 503"):
            await _call(session, enphase_mcp.get_battery_mode)

    async def test_raises_on_iserror_flag(self):
        session = make_mock_session("whatever", is_error=True)
        with pytest.raises(RuntimeError):
            await _call(session, enphase_mcp.get_battery_mode)


class TestSetBatteryMode:

    async def test_returns_parsed_dict_on_success(self):
        session = make_mock_session('{"profile_set": "self-consumption"}')
        result = await _call(session, enphase_mcp.set_battery_mode, "self-consumption")
        assert result == {"profile_set": "self-consumption"}

    async def test_raises_on_status_error_dict(self):
        session = make_mock_session('{"status": "error", "message": "token expired"}')
        with pytest.raises(RuntimeError, match="token expired"):
            await _call(session, enphase_mcp.set_battery_mode, "self-consumption")

    async def test_passes_profile_argument(self):
        session = make_mock_session('{"profile_set": "cost_savings"}')
        await _call(session, enphase_mcp.set_battery_mode, "cost_savings")
        session.call_tool.assert_called_once_with(
            "enphase_set_battery_profile", {"profile": "cost_savings"}
        )
