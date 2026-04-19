# enphase-juicebox-coordinator — Roadmap

## Current Milestone
End-to-end operational — coordinator fetching Enphase tariff, computing schedule, and programming JuiceBox via MCP

### 🔨 In Progress
[Empty]

### 🟢 Ready (Next Up)
[Empty]

### 📋 Backlog
- Add solar production awareness — prefer charging when production exceeds consumption
- Tune optimizer thresholds based on real-world Arizona TOU schedule
- Add override tool: manual "charge now" regardless of TOU window
- Add daily scheduling report delivered via notification or log

### 🔴 Blocked
[Empty]

## ✅ Completed
- **Fix tariff parsing (2026-04-19)** — `optimizer.py` now handles real Enphase `purchase.seasons[].days[].periods[]` format (minutes from midnight). `_active_season()` uses range-based matching when `endMonth` is present, "last season with start_month ≤ today" for legacy format. APS fallback corrected to 16:00–19:00. All 46 tests passing.
- **Weekly automated image rebuild (2026-04-19)** — `build.yml` adds `cron: "0 4 * * 0"` for weekly Sunday base-image maintenance, matching claude-juicebox pattern.
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
