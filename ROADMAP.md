# enphase-juicebox-coordinator — Roadmap

## Current Milestone
End-to-end operational — coordinator fetching Enphase tariff, computing schedule, and programming JuiceBox via MCP

### 🔨 In Progress
[Empty]

### 🟢 Ready (Next Up)
[Empty]

### 📋 Backlog
[Empty]

### 🔴 Blocked
[Empty]

## ✅ Completed
- **Surplus solar monitor (2026-04-20)** — 15-minute polling job detects when battery SOC ≥ 95% and solar production exceeds home consumption, then activates JuiceBox at computed amps (surplus watts ÷ 240V). Reverts to TOU schedule when surplus ends. Peak hours are excluded so the car never charges during expensive windows. New `get_surplus_status` MCP tool exposes current mode, SOC, production/consumption, and thresholds. Data sourced from `enphase_get_energy_summary` (15-min interval arrays + real-time `battery_details.aggregate_soc`).
- **Override tool `charge_now` (2026-04-19)** — MCP tool that pushes an immediate charging window for today (optional `hours` param; defaults to until 23:59). Normal TOU schedule resumes at next 04:00 run.
- **Weekly Sunday report + email (2026-04-19)** — Coordinator generates report at Sunday 06:00 Arizona (logs + `get_weekly_report` tool + `/report` HTTP endpoint). Claude Code scheduled task fires at 07:17 Sunday, fetches `/report`, and emails digest to charles.aldarondo@gmail.com via Gmail MCP. Flags drift/errors prominently. 59 tests passing.
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
  - Connected to Claude Desktop at `http://<YOUR-NAS-IP>:8767/sse`
- Coordinator orchestration logic (`coordinator.py`)
- Peak window optimizer (`optimizer.py`)
- JuiceBox MCP caller (`juicebox_mcp.py`)
- MCP server entry point (`server.py`)
- Full pytest test suite with async mocking
- Dockerfile for NAS deployment

## 🚫 Blocked
- ❌ [docker-monitor:container-stopped] Container `enphase-juicebox-coordinator` is not running on the NAS — check `docker logs enphase-juicebox-coordinator` and restart — 2026-04-21 08:42 UTC
- ❌ [docker-monitor:deploy-failed] GitHub Actions deploy failed (run #24690313002) — https://github.com/aldarondo/enphase-juicebox-coordinator/actions/runs/24690313002 — 2026-04-21 08:00 UTC
