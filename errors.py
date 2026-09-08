"""
Exception rendering helpers.

Why this module exists
----------------------
The MCP SSE client (``mcp.client.sse.sse_client`` + ``ClientSession``) runs its
transport inside an anyio task group. When anything inside fails — a DNS
failure, a refused connection, a read timeout — the ``async with`` blocks exit
by raising a ``BaseExceptionGroup``, whose ``str()`` is the notoriously useless:

    unhandled errors in a TaskGroup (1 sub-exception)

That string is what landed in the coordinator's logs and in the "mode switch
FAILED" alert emails, hiding the actual cause. On 2026-09-03 the NAS lost
outbound DNS and every alert for the next five days said only "TaskGroup
(1 sub-exception)" — the real error, ``httpx.ConnectError: [Errno -3]
Temporary failure in name resolution``, was sitting one level down inside the
group the whole time.

A second, related trap: some exceptions have an *empty* ``str()``.
``httpx.ConnectTimeout()`` is the common one, which is why the logs showed
``Failed to send failure alert email:`` with nothing after the colon.

``describe_exception`` handles both: it flattens exception groups to their
leaves and falls back to the class name whenever a message is empty.
"""

import asyncio
import contextlib

__all__ = [
    "UpstreamError",
    "iter_leaf_exceptions",
    "describe_exception",
    "flatten_exception",
    "surfacing_errors",
]

# Longest description we will build before truncating, so a group with many
# sub-exceptions can never produce an unbounded log line or email body.
_MAX_DESCRIPTION_CHARS = 600


class UpstreamError(RuntimeError):
    """An upstream call failed; the message carries the flattened real cause."""


def iter_leaf_exceptions(exc: BaseException):
    """Yield the non-group leaf exceptions inside ``exc``, depth-first.

    A plain exception yields just itself. Groups are walked recursively, since
    anyio can nest a group inside a group.
    """
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from iter_leaf_exceptions(sub)
    else:
        yield exc


def _describe_one(exc: BaseException) -> str:
    """Render a single leaf as ``TypeName: message``, or ``TypeName`` if empty.

    When the message is empty we look at ``__cause__``/``__context__`` before
    giving up — ``httpx`` timeouts in particular are bare, but the underlying
    ``httpcore`` error usually says something useful.
    """
    name = type(exc).__name__
    message = str(exc).strip()
    if not message:
        for chained in (exc.__cause__, exc.__context__):
            if chained is not None and str(chained).strip():
                return f"{name}: {type(chained).__name__}: {str(chained).strip()}"
        return name
    return f"{name}: {message}"


def describe_exception(exc: BaseException) -> str:
    """Render ``exc`` as a single actionable line, flattening exception groups.

    Duplicate leaf descriptions are collapsed — a task group that fails the same
    way in five concurrent tasks should read as one cause, not five.
    """
    seen: list[str] = []
    for leaf in iter_leaf_exceptions(exc):
        described = _describe_one(leaf)
        if described not in seen:
            seen.append(described)

    if not seen:
        # A group with no sub-exceptions should be impossible, but never render
        # an empty string into an alert email.
        return _describe_one(exc)

    joined = "; ".join(seen)
    if len(joined) > _MAX_DESCRIPTION_CHARS:
        joined = joined[: _MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return joined


def flatten_exception(exc: BaseException, context: str | None = None) -> BaseException:
    """Return the exception that should be raised in place of ``exc``.

    Rules, in order:
      * A single-leaf group collapses to that leaf, preserving its type so
        callers can still catch ``httpx.ConnectError`` and friends.
      * Anything else that is a group, or whose message is empty, becomes an
        ``UpstreamError`` carrying the flattened description.
      * A normal exception with a usable message is returned untouched, so this
        never degrades errors that were already clear.
    """
    is_group = isinstance(exc, BaseExceptionGroup)

    if is_group:
        leaves = list(iter_leaf_exceptions(exc))
        if len(leaves) == 1 and str(leaves[0]).strip():
            return leaves[0]

    if not is_group and str(exc).strip():
        return exc

    described = describe_exception(exc)
    return UpstreamError(f"{context}: {described}" if context else described)


@contextlib.asynccontextmanager
async def surfacing_errors(context: str):
    """Re-raise anything escaping the block with its real cause made visible.

    Cancellation is never rewritten — a ``CancelledError``, including one
    wrapped in a group by anyio, must keep propagating as cancellation or the
    scheduler's shutdown path breaks.
    """
    try:
        yield
    except BaseException as exc:
        if any(isinstance(leaf, asyncio.CancelledError)
               for leaf in iter_leaf_exceptions(exc)):
            raise
        raise flatten_exception(exc, context) from exc
