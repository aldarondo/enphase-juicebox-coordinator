"""
Tests for errors.py — flattening anyio exception groups into actionable text.

These cover the 2026-09-03 failure mode: the NAS lost outbound DNS, every MCP
call failed inside an anyio task group, and five days of alert emails said only
"unhandled errors in a TaskGroup (1 sub-exception)".
"""

import asyncio

import httpx
import pytest

from errors import (
    UpstreamError,
    describe_exception,
    flatten_exception,
    iter_leaf_exceptions,
    surfacing_errors,
)

# The exact string anyio produces, and the one that reached the alert emails.
TASKGROUP_STR = "unhandled errors in a TaskGroup (1 sub-exception)"
DNS_MESSAGE = "[Errno -3] Temporary failure in name resolution"


def _dns_group() -> BaseExceptionGroup:
    return ExceptionGroup(TASKGROUP_STR, [httpx.ConnectError(DNS_MESSAGE)])


# ── iter_leaf_exceptions ──────────────────────────────────────────────────────

def test_plain_exception_is_its_own_leaf():
    exc = ValueError("boom")
    assert list(iter_leaf_exceptions(exc)) == [exc]


def test_nested_groups_are_flattened_depth_first():
    inner = ExceptionGroup("inner", [ValueError("a"), KeyError("b")])
    outer = ExceptionGroup("outer", [inner, TypeError("c")])
    kinds = [type(e).__name__ for e in iter_leaf_exceptions(outer)]
    assert kinds == ["ValueError", "KeyError", "TypeError"]


# ── describe_exception ────────────────────────────────────────────────────────

def test_describes_real_cause_instead_of_taskgroup_noise():
    described = describe_exception(_dns_group())
    assert described == f"ConnectError: {DNS_MESSAGE}"
    assert "TaskGroup" not in described


def test_empty_message_falls_back_to_class_name():
    """httpx timeouts stringify to "" — the cause of the blank email-failure log."""
    assert describe_exception(httpx.ConnectTimeout("")) == "ConnectTimeout"


def test_empty_message_prefers_chained_cause_when_present():
    try:
        try:
            raise OSError("underlying socket failure")
        except OSError as inner:
            raise httpx.ConnectTimeout("") from inner
    except httpx.ConnectTimeout as exc:
        described = describe_exception(exc)
    assert described == "ConnectTimeout: OSError: underlying socket failure"


def test_clear_exception_is_left_intact():
    assert describe_exception(RuntimeError("token expired")) == "RuntimeError: token expired"


def test_duplicate_leaf_causes_are_collapsed():
    group = ExceptionGroup(TASKGROUP_STR, [httpx.ConnectError(DNS_MESSAGE) for _ in range(4)])
    assert describe_exception(group) == f"ConnectError: {DNS_MESSAGE}"


def test_distinct_causes_are_all_reported():
    group = ExceptionGroup("boom", [httpx.ConnectError(DNS_MESSAGE), ValueError("bad payload")])
    described = describe_exception(group)
    assert f"ConnectError: {DNS_MESSAGE}" in described
    assert "ValueError: bad payload" in described


def test_description_is_length_capped():
    group = ExceptionGroup("boom", [ValueError("x" * 400), KeyError("y" * 400)])
    assert len(describe_exception(group)) <= 600


def test_empty_group_never_renders_an_empty_string():
    """A degenerate group must still produce something printable in an alert."""

    class _EmptyGroup(BaseExceptionGroup):
        def __new__(cls):
            # Build a real group, then present it as if it had no sub-exceptions.
            self = BaseExceptionGroup.__new__(cls, "empty", [ValueError("x")])
            return self

        @property
        def exceptions(self):
            return ()

    assert describe_exception(_EmptyGroup()) != ""


# ── flatten_exception ─────────────────────────────────────────────────────────

def test_single_leaf_group_collapses_to_the_leaf_preserving_type():
    """Type preservation matters so callers can still catch httpx errors."""
    flat = flatten_exception(_dns_group(), "enphase_get_battery_settings")
    assert isinstance(flat, httpx.ConnectError)
    assert str(flat) == DNS_MESSAGE


def test_multi_leaf_group_becomes_upstream_error_with_context():
    group = ExceptionGroup("boom", [ValueError("a"), KeyError("b")])
    flat = flatten_exception(group, "enphase_get_tariff")
    assert isinstance(flat, UpstreamError)
    assert str(flat).startswith("enphase_get_tariff: ")
    assert "ValueError: a" in str(flat)


def test_already_clear_exception_is_returned_unchanged():
    exc = RuntimeError("enphase_get_tariff failed: token expired")
    assert flatten_exception(exc, "ctx") is exc


def test_empty_message_exception_gains_context():
    flat = flatten_exception(httpx.ConnectTimeout(""), "enphase_get_tariff")
    assert isinstance(flat, UpstreamError)
    assert str(flat) == "enphase_get_tariff: ConnectTimeout"


# ── surfacing_errors ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_surfacing_errors_unwraps_group_to_real_cause():
    with pytest.raises(httpx.ConnectError) as caught:
        async with surfacing_errors("enphase_get_battery_settings"):
            raise _dns_group()
    assert str(caught.value) == DNS_MESSAGE


@pytest.mark.asyncio
async def test_surfacing_errors_passes_clear_errors_through_untouched():
    original = RuntimeError("enphase_get_tariff failed: token expired")
    with pytest.raises(RuntimeError) as caught:
        async with surfacing_errors("enphase_get_tariff"):
            raise original
    assert caught.value is original


@pytest.mark.asyncio
async def test_surfacing_errors_never_rewrites_cancellation():
    """Rewriting a CancelledError would break the scheduler's shutdown path."""
    with pytest.raises(asyncio.CancelledError):
        async with surfacing_errors("enphase_get_tariff"):
            raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_surfacing_errors_preserves_cancellation_inside_a_group():
    with pytest.raises(BaseExceptionGroup):
        async with surfacing_errors("enphase_get_tariff"):
            raise BaseExceptionGroup("cancelled", [asyncio.CancelledError()])


@pytest.mark.asyncio
async def test_surfacing_errors_is_transparent_on_success():
    async with surfacing_errors("enphase_get_tariff"):
        value = 42
    assert value == 42
