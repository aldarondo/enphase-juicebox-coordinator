"""
Tests for battery_mode.py — mocks enphase_mcp and email_mcp.
"""

import os
import sys

import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import battery_mode  # noqa: E402 — imported after sys.path is set


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Skip real sleep between retry attempts so tests are fast."""
    async def _instant_sleep(_s):
        return None
    monkeypatch.setattr("battery_mode.asyncio.sleep", _instant_sleep)


@pytest.fixture
def mock_email(monkeypatch):
    m = AsyncMock(return_value={"sent": True})
    monkeypatch.setattr("battery_mode.email_mcp.send_email", m)
    return m


# ===========================================================================
# Happy path
# ===========================================================================

class TestSwitchHappyPath:

    @pytest.fixture
    def mocks(self, monkeypatch, mock_email):
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.get_battery_mode",
            AsyncMock(return_value={"mode": "savings"}),
        )
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.set_battery_mode",
            AsyncMock(return_value={"mode": "self-consumption"}),
        )
        return mock_email

    async def test_status_ok(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["status"] == "ok"

    async def test_applied_mode_matches(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["applied_mode"] == "self-consumption"

    async def test_current_mode_recorded(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["current_mode"] == "savings"

    async def test_single_attempt(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["attempts"] == 1

    async def test_errors_empty(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["errors"] == []

    async def test_no_email_sent_on_success(self, mocks):
        await battery_mode.switch_to_self_consumption()
        mocks.assert_not_called()


# ===========================================================================
# Already in target mode → skipped
# ===========================================================================

class TestAlreadyInTargetMode:

    @pytest.fixture
    def mocks(self, monkeypatch, mock_email):
        set_mock = AsyncMock(return_value={"mode": "self-consumption"})
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.get_battery_mode",
            AsyncMock(return_value={"mode": "self-consumption"}),
        )
        monkeypatch.setattr("battery_mode.enphase_mcp.set_battery_mode", set_mock)
        return {"email": mock_email, "set": set_mock}

    async def test_status_skipped(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["status"] == "skipped_already_target"

    async def test_set_not_called(self, mocks):
        await battery_mode.switch_to_self_consumption()
        mocks["set"].assert_not_called()

    async def test_no_email_on_skip(self, mocks):
        await battery_mode.switch_to_self_consumption()
        mocks["email"].assert_not_called()


# ===========================================================================
# First attempt fails, retry succeeds → ok
# ===========================================================================

class TestRetrySucceeds:

    @pytest.fixture
    def mocks(self, monkeypatch, mock_email):
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.get_battery_mode",
            AsyncMock(side_effect=[Exception("transient"), {"mode": "savings"}]),
        )
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.set_battery_mode",
            AsyncMock(return_value={"mode": "self-consumption"}),
        )
        return mock_email

    async def test_status_ok_after_retry(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["status"] == "ok"

    async def test_two_attempts_recorded(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["attempts"] == 2

    async def test_first_error_recorded(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert any("transient" in e for e in result["errors"])

    async def test_no_email_sent(self, mocks):
        await battery_mode.switch_to_self_consumption()
        mocks.assert_not_called()


# ===========================================================================
# Both attempts fail → error + email alert
# ===========================================================================

class TestBothAttemptsFail:

    @pytest.fixture
    def mocks(self, monkeypatch, mock_email):
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.get_battery_mode",
            AsyncMock(side_effect=Exception("enphase down")),
        )
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.set_battery_mode",
            AsyncMock(return_value={"mode": "self-consumption"}),
        )
        return mock_email

    async def test_status_error(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["status"] == "error"

    async def test_two_attempts(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["attempts"] == 2

    async def test_both_errors_recorded(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert len(result["errors"]) == 2

    async def test_email_sent_on_both_fail(self, mocks):
        await battery_mode.switch_to_self_consumption()
        mocks.assert_called_once()

    async def test_email_subject_mentions_label(self, mocks):
        await battery_mode.switch_to_self_consumption()
        kwargs = mocks.call_args.kwargs
        assert "15:57 pre-peak" in kwargs["subject"]

    async def test_email_subject_mentions_retries_exhausted(self, mocks):
        await battery_mode.switch_to_self_consumption()
        kwargs = mocks.call_args.kwargs
        assert "retries exhausted" in kwargs["subject"].lower()

    async def test_email_body_mentions_manual_intervention(self, mocks):
        await battery_mode.switch_to_self_consumption()
        kwargs = mocks.call_args.kwargs
        assert "manual intervention" in kwargs["body"].lower()

    async def test_email_body_mentions_consequence(self, mocks):
        await battery_mode.switch_to_self_consumption()
        kwargs = mocks.call_args.kwargs
        assert "battery will cycle" in kwargs["body"].lower() or "cycle" in kwargs["body"].lower()

    async def test_email_no_hardcoded_recipient(self, mocks):
        """send_email must not hardcode 'to' — recipient comes from ALERT_TO_EMAIL env var."""
        await battery_mode.switch_to_self_consumption()
        kwargs = mocks.call_args.kwargs
        assert "to" not in kwargs, "recipient should not be hardcoded; let email_mcp use ALERT_TO_EMAIL"


# ===========================================================================
# Set succeeds but confirms wrong mode → retry; if second also wrong → error
# ===========================================================================

class TestSetReturnsWrongMode:

    @pytest.fixture
    def mocks(self, monkeypatch, mock_email):
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.get_battery_mode",
            AsyncMock(return_value={"mode": "savings"}),
        )
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.set_battery_mode",
            AsyncMock(return_value={"mode": "backup-only"}),
        )
        return mock_email

    async def test_status_error(self, mocks):
        result = await battery_mode.switch_to_self_consumption()
        assert result["status"] == "error"

    async def test_email_sent(self, mocks):
        await battery_mode.switch_to_self_consumption()
        mocks.assert_called_once()


# ===========================================================================
# Post-peak switch (Self-Consumption → Savings)
# ===========================================================================

class TestPostPeakSwitch:

    @pytest.fixture
    def mocks(self, monkeypatch, mock_email):
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.get_battery_mode",
            AsyncMock(return_value={"usage": "self-consumption"}),
        )
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.set_battery_mode",
            AsyncMock(return_value={"profile_set": "cost_savings"}),
        )
        return mock_email

    async def test_status_ok(self, mocks):
        result = await battery_mode.switch_to_savings()
        assert result["status"] == "ok"

    async def test_target_mode_savings(self, mocks):
        result = await battery_mode.switch_to_savings()
        assert result["target_mode"] == "cost_savings"

    async def test_label_is_post_peak(self, mocks):
        result = await battery_mode.switch_to_savings()
        assert "19:02" in result["label"]


# ===========================================================================
# Email send itself fails → switch result still reflects error (no crash)
# ===========================================================================

class TestEmailAlertFails:

    async def test_switch_result_still_error_if_email_fails(self, monkeypatch):
        monkeypatch.setattr(
            "battery_mode.enphase_mcp.get_battery_mode",
            AsyncMock(side_effect=Exception("enphase down")),
        )
        monkeypatch.setattr(
            "battery_mode.email_mcp.send_email",
            AsyncMock(side_effect=Exception("email server down")),
        )
        result = await battery_mode.switch_to_self_consumption()
        assert result["status"] == "error"
        assert result["attempts"] == 2


# ===========================================================================
# Set response missing "mode" field → verifies via a follow-up read
# ===========================================================================

class TestSetResponseMissingMode:
    """
    Newer Enphase API responses could look like {"status": "ok", "queued": true}
    with no mode echoed back. The switcher must verify via an independent read
    rather than optimistically assuming success.
    """

    async def test_verifies_via_read_and_passes(self, monkeypatch, mock_email):
        get_mock = AsyncMock(side_effect=[
            {"mode": "savings"},            # initial read
            {"mode": "self-consumption"},   # verify read after ambiguous set
        ])
        set_mock = AsyncMock(return_value={"status": "ok", "queued": True})
        monkeypatch.setattr("battery_mode.enphase_mcp.get_battery_mode", get_mock)
        monkeypatch.setattr("battery_mode.enphase_mcp.set_battery_mode", set_mock)

        result = await battery_mode.switch_to_self_consumption()

        assert result["status"] == "ok"
        assert result["applied_mode"] == "self-consumption"
        assert get_mock.call_count == 2  # initial + verify
        mock_email.assert_not_called()

    async def test_verify_read_shows_wrong_mode_triggers_retry_and_email(self, monkeypatch, mock_email):
        """
        Set returns no mode; verify read shows mode was NOT actually applied.
        Both attempts fail the same way → email alert.
        """
        get_mock = AsyncMock(side_effect=[
            {"mode": "savings"},    # attempt 1 initial
            {"mode": "savings"},    # attempt 1 verify (still savings — set didn't take)
            {"mode": "savings"},    # attempt 2 initial
            {"mode": "savings"},    # attempt 2 verify
        ])
        set_mock = AsyncMock(return_value={"queued": True})
        monkeypatch.setattr("battery_mode.enphase_mcp.get_battery_mode", get_mock)
        monkeypatch.setattr("battery_mode.enphase_mcp.set_battery_mode", set_mock)

        result = await battery_mode.switch_to_self_consumption()

        assert result["status"] == "error"
        assert result["attempts"] == 2
        mock_email.assert_called_once()

    async def test_both_responses_missing_mode_treated_as_failure(self, monkeypatch, mock_email):
        """Set returns no mode AND verify read also returns no mode → failure."""
        get_mock = AsyncMock(side_effect=[
            {"mode": "savings"},           # initial
            {"status": "unknown"},         # verify (no mode key)
            {"mode": "savings"},           # retry initial
            {"status": "unknown"},         # retry verify
        ])
        set_mock = AsyncMock(return_value={"queued": True})
        monkeypatch.setattr("battery_mode.enphase_mcp.get_battery_mode", get_mock)
        monkeypatch.setattr("battery_mode.enphase_mcp.set_battery_mode", set_mock)

        result = await battery_mode.switch_to_self_consumption()

        assert result["status"] == "error"
        mock_email.assert_called_once()


# ===========================================================================
# _extract_mode helper
# ===========================================================================

class TestExtractMode:

    def test_string_payload(self):
        assert battery_mode._extract_mode("cost_savings") == "cost_savings"

    def test_dict_usage_key(self):
        # enphase_get_battery_settings raw API response
        assert battery_mode._extract_mode({"usage": "cost_savings"}) == "cost_savings"

    def test_dict_profile_set_key(self):
        # enphase_set_battery_profile server wrapper response
        assert battery_mode._extract_mode({"profile_set": "self-consumption"}) == "self-consumption"

    def test_dict_mode_key(self):
        assert battery_mode._extract_mode({"mode": "self-consumption"}) == "self-consumption"

    def test_dict_battery_mode_key(self):
        assert battery_mode._extract_mode({"battery_mode": "cost_savings"}) == "cost_savings"

    def test_dict_profile_key(self):
        assert battery_mode._extract_mode({"profile": "cost_savings"}) == "cost_savings"

    def test_none_when_missing(self):
        assert battery_mode._extract_mode({"other": "x"}) is None

    def test_none_when_nondict(self):
        assert battery_mode._extract_mode(42) is None
