# enphase-juicebox-coordinator

## What This Project Is
Smart coordinator service that fetches TOU (time-of-use) tariffs from Enphase, identifies peak pricing windows, and programs the JuiceBox EV charger to avoid charging during expensive periods. Orchestrates between the Enphase Enlighten API for tariff data, custom optimization logic, and JuiceBox MCP tools. Returns structured scheduling reports with reasoning.

## Tech Stack
- Python 3.11+
- MCP SDK (Model Context Protocol)
- APScheduler (scheduling)
- pytz (timezone handling — Arizona TOU)
- httpx / respx (async HTTP + mocking)
- pytest
- Docker (NAS deployment)

## Key Decisions
- Coordinator pattern: fetches from Enphase, runs optimizer, calls JuiceBox MCP tools
- Optimization logic is isolated in `optimizer.py` for testability
- Arizona TOU rates are the primary use case (Tucson Electric Power or similar)
- Depends on both claude-enphase and claude-juicebox being deployed

## Session Startup Checklist
1. Read ROADMAP.md to find the current active task
2. Check MEMORY.md if it exists — it contains auto-saved learnings from prior sessions
3. Ensure claude-enphase and claude-juicebox are running before testing coordinator
4. Run `pip install -r requirements.txt` if dependencies are stale
5. Run `pytest` to verify tests pass before making changes
6. Do not make architectural changes without confirming with Charles first

## Key Files
- `coordinator.py` — main orchestration logic
- `enphase.py` — Enphase API client
- `optimizer.py` — TOU peak window detection and scheduling logic
- `juicebox_mcp.py` — JuiceBox MCP tool caller
- `server.py` — MCP server entry point
- `Dockerfile` — NAS deployment
- `tests/` — pytest test suite

---
@~/Documents/GitHub/CLAUDE.md
