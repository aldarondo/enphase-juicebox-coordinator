"""
Tests for juicebox_mcp.py — mocks sse_client and ClientSession.
"""

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import juicebox_mcp  # noqa: E402

DEFAULT_URL = "http://<YOUR-NAS-IP>:3001/sse"

SAMPLE_SCHEDULE = [
    {"label": "weekday", "days": ["mon"], "start": "20:00", "end": "15:00", "max_amps": 32}
]


# ---------------------------------------------------------------------------
# Helper to build a mock session
# ---------------------------------------------------------------------------

def make_mock_session(response_text: str | None = '{"success": true}', empty_content: bool = False):
    """
    Build a mock MCP ClientSession whose call_tool returns a result
    with content[0].text == response_text, or empty content if empty_content=True.
    """
    result = MagicMock()
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


def make_sse_context(session):
    """Return an async context manager that yields (read, write) and patches ClientSession."""
    read_mock = AsyncMock()
    write_mock = AsyncMock()

    sse_cm = MagicMock()
    sse_cm.__aenter__ = AsyncMock(return_value=(read_mock, write_mock))
    sse_cm.__aexit__ = AsyncMock(return_value=False)
    return sse_cm


# ===========================================================================
# set_charging_schedule tests
# ===========================================================================

class TestSetChargingSchedule:

    async def test_calls_set_charging_schedule_tool_with_correct_payload(self):
        session = make_mock_session('{"ok": true}')
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            await juicebox_mcp.set_charging_schedule(SAMPLE_SCHEDULE)

        session.call_tool.assert_called_once_with(
            "set_charging_schedule", {"schedule": SAMPLE_SCHEDULE}
        )

    async def test_returns_parsed_dict_from_valid_json_response(self):
        session = make_mock_session('{"status": "ok", "windows": 2}')
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            result = await juicebox_mcp.set_charging_schedule(SAMPLE_SCHEDULE)

        assert result == {"status": "ok", "windows": 2}

    async def test_returns_raw_when_response_text_is_not_valid_json(self):
        non_json = "Schedule updated successfully"
        session = make_mock_session(non_json)
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            result = await juicebox_mcp.set_charging_schedule(SAMPLE_SCHEDULE)

        assert result == {"raw": non_json}

    async def test_returns_success_true_when_content_is_empty(self):
        session = make_mock_session(empty_content=True)
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            result = await juicebox_mcp.set_charging_schedule(SAMPLE_SCHEDULE)

        assert result == {"success": True}

    async def test_connects_to_default_juicebox_mcp_url(self, monkeypatch):
        """When JUICEBOX_MCP_URL is not overridden, the default URL is used."""
        monkeypatch.setattr("juicebox_mcp.JUICEBOX_MCP_URL", DEFAULT_URL)

        session = make_mock_session()
        sse_cm = make_sse_context(session)
        captured_urls = []

        def fake_sse_client(url):
            captured_urls.append(url)
            return sse_cm

        with patch("juicebox_mcp.sse_client", side_effect=fake_sse_client), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            await juicebox_mcp.set_charging_schedule(SAMPLE_SCHEDULE)

        assert captured_urls == [DEFAULT_URL]

    async def test_uses_custom_juicebox_mcp_url(self, monkeypatch):
        custom_url = "http://10.0.0.5:9000/sse"
        monkeypatch.setattr("juicebox_mcp.JUICEBOX_MCP_URL", custom_url)

        session = make_mock_session()
        sse_cm = make_sse_context(session)
        captured_urls = []

        def fake_sse_client(url):
            captured_urls.append(url)
            return sse_cm

        with patch("juicebox_mcp.sse_client", side_effect=fake_sse_client), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            await juicebox_mcp.set_charging_schedule(SAMPLE_SCHEDULE)

        assert captured_urls == [custom_url]

    async def test_session_initialize_is_called(self):
        session = make_mock_session()
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            await juicebox_mcp.set_charging_schedule(SAMPLE_SCHEDULE)

        session.initialize.assert_called_once()


# ===========================================================================
# get_charger_status tests
# ===========================================================================

class TestGetChargerStatus:

    async def test_calls_get_charger_status_tool(self):
        session = make_mock_session('{"charging": true, "amps": 24}')
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            await juicebox_mcp.get_charger_status()

        session.call_tool.assert_called_once_with("get_charger_status", {})

    async def test_returns_parsed_dict_from_response(self):
        session = make_mock_session('{"charging": true, "amps": 24}')
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            result = await juicebox_mcp.get_charger_status()

        assert result == {"charging": True, "amps": 24}

    async def test_returns_empty_dict_when_content_is_empty(self):
        session = make_mock_session(empty_content=True)
        sse_cm = make_sse_context(session)

        with patch("juicebox_mcp.sse_client", return_value=sse_cm), \
             patch("juicebox_mcp.ClientSession", return_value=session):
            result = await juicebox_mcp.get_charger_status()

        assert result == {}
