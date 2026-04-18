"""
Enphase–JuiceBox Coordinator MCP Server

Exposes two tools so Claude can trigger and inspect the coordinator:

  run_coordinator  — fetch Enphase rates + battery SOC, compute optimal
                     EV charging windows, push schedule to JuiceBox MCP.

  get_last_run     — return the result from the most recent coordinator run
                     (whether triggered manually or by the daily scheduler).

Also runs a background APScheduler job (daily at 04:00 Arizona time) so
the JuiceBox schedule stays current without Claude needing to be involved.

Transport modes (set MCP_TRANSPORT env var):
  stdio (default) — Claude Code subprocess; use for local dev
  sse             — persistent HTTP server on MCP_PORT (default 8767);
                    use for NAS/Docker deployment so the scheduler runs 24/7

Add to Claude Desktop (SSE, NAS deployment):
  "coordinator": { "type": "sse", "url": "http://<NAS>:8767/sse" }
"""

import asyncio
import json
import logging
import os
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import coordinator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coordinator-mcp")
ARIZONA = pytz.timezone("America/Phoenix")

app = Server("enphase-juicebox-coordinator")

# ── Shared state ──────────────────────────────────────────────────────────────
_last_result: dict | None = None

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="run_coordinator",
        description=(
            "Fetch the current Enphase TOU rate schedule and home battery SOC, "
            "compute the optimal JuiceBox EV charging windows for the week "
            "(preferring cheapest rate hours, scaling amps by battery level), "
            "and push the schedule to the JuiceBox MCP server. "
            "Returns the full result including the computed schedule and reasoning."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_last_run",
        description=(
            "Return the result from the most recent coordinator run — "
            "the computed schedule, reasoning, battery SOC used, and any errors. "
            "Useful for checking what schedule is currently programmed and why."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]

# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _last_result

    if name == "run_coordinator":
        log.info("Tool: run_coordinator triggered by Claude")
        try:
            _last_result = await coordinator.run()
        except Exception as exc:
            log.exception("run_coordinator failed")
            _last_result = {"status": "error", "error": str(exc)}
        return [TextContent(type="text", text=json.dumps(_last_result, indent=2))]

    if name == "get_last_run":
        if _last_result is None:
            payload = {
                "status":  "never_run",
                "message": "The coordinator has not run yet in this session. "
                           "Call run_coordinator to trigger it, or wait for the "
                           "daily 04:00 scheduled run.",
            }
        else:
            payload = _last_result
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _scheduled_run():
    global _last_result
    log.info("[scheduler] Daily coordinator run triggered (04:00 Arizona)")
    try:
        _last_result = await coordinator.run()
        log.info("[scheduler] Done — status: %s", _last_result.get("status"))
    except Exception:
        log.exception("[scheduler] Daily run failed")


def _build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ARIZONA)
    scheduler.add_job(
        _scheduled_run,
        "cron",
        hour=4,
        minute=0,
        id="daily_coordinator",
    )
    return scheduler

# ── Entry point ───────────────────────────────────────────────────────────────

async def _run_stdio():
    scheduler = _build_scheduler()
    scheduler.start()
    next_run = scheduler.get_job("daily_coordinator").next_run_time
    log.info("Coordinator MCP server starting (stdio)")
    log.info("  Daily scheduler: next run at %s (America/Phoenix)", next_run)
    log.info("  JuiceBox MCP:    %s", os.getenv("JUICEBOX_MCP_URL", "http://192.168.0.64:3001/sse"))

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

    scheduler.shutdown()


def _run_sse(host: str, port: int):
    from contextlib import asynccontextmanager
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    import uvicorn

    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())

    @asynccontextmanager
    async def lifespan(starlette_app):
        scheduler = _build_scheduler()
        scheduler.start()
        next_run = scheduler.get_job("daily_coordinator").next_run_time
        log.info("Coordinator MCP server starting (SSE) on %s:%d", host, port)
        log.info("  Daily scheduler: next run at %s (America/Phoenix)", next_run)
        log.info("  JuiceBox MCP:    %s", os.getenv("JUICEBOX_MCP_URL", "http://192.168.0.64:3001/sse"))
        yield
        scheduler.shutdown()

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
        lifespan=lifespan,
    )
    uvicorn.run(starlette_app, host=host, port=port)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8767"))
        _run_sse(host, port)
    else:
        asyncio.run(_run_stdio())
