# enphase-juicebox-coordinator — Roadmap

## Current Milestone
End-to-end operational — coordinator fetching Enphase tariff, computing schedule, and programming JuiceBox via MCP

### 🔨 In Progress
[Empty]

### 🟢 Ready (Next Up)
- **Fix tariff parsing** — `enphase_get_tariff` returns a processed summary; `optimizer.py` expects raw Enlighten API JSON (`{"tariff": {"seasons": [...]}}`). Need to inspect actual tool response and either update `optimizer.py` or add an adapter in `enphase_mcp.py`. Currently falls back to hardcoded APS defaults (15:00–20:00 weekdays), which is functionally correct but ignores real TEP/APS rate data.

### 📋 Backlog
- Add solar production awareness — prefer charging when production exceeds consumption
- Tune optimizer thresholds based on real-world Arizona TOU schedule
- Add override tool: manual "charge now" regardless of TOU window
- Add daily scheduling report delivered via notification or log

### 🔴 Blocked
[Empty]

## ✅ Completed
- **End-to-end test passing (2026-04-19)** — `status=ok`, `juicebox_ok=True`
  - Coordinator fetches tariff from Enphase MCP, computes 2-window schedule, programs JuiceBox MCP
  - JuiceBox confirms: `windows_scheduled=2`, `cron_jobs_created=4`
- **Refactor: coordinator now uses both upstream MCPs (2026-04-19)**
  - Replaced direct Enphase API calls (`enphase.py`) with `enphase_mcp.py` (calls claude-enphase at `:8766/sse`)
  - JuiceBox scheduling via `juicebox_mcp.py` (calls claude-juicebox at `:3001/sse`)
- **GHCR deployment pipeline (2026-04-19)**
  - Both `enphase-juicebox-coordinator` and `claude-juicebox-mcp` build to GHCR on push
  - NAS pulls pre-built images; no local Docker build required
  - Fixed Node.js MCP SDK version mismatch (`^1.0.0` → `^1.9.0`)
  - Fixed SDK 1.29 breaking change: `handlePostMessage(req, res, req.body)`
  - Fixed SDK 1.29 breaking change: per-connection `McpServer` factory in `server.js`
- **NAS deployment (2026-04-18)**
  - Added SSE transport mode to `server.py`
  - Created `docker-compose.yml` + updated `Dockerfile` and `requirements.txt`
  - Deployed to `/volume1/docker/enphase-juicebox-coordinator` at port 8767
  - Daily scheduler running: 04:00 America/Phoenix
  - Connected to Claude Desktop at `http://192.168.0.64:8767/sse`
- Coordinator orchestration logic (`coordinator.py`)
- Peak window optimizer (`optimizer.py`)
- JuiceBox MCP caller (`juicebox_mcp.py`)
- MCP server entry point (`server.py`)
- Full pytest test suite with async mocking
- Dockerfile for NAS deployment
