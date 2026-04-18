# enphase-juicebox-coordinator — Roadmap

## Current Milestone
End-to-end deployment — coordinator running on NAS, auto-scheduling EV charging around TOU peak hours

### 🔨 In Progress
[Empty]

### 🟢 Ready (Next Up)
- End-to-end test: trigger `run_coordinator` via Claude Desktop and verify JuiceBox schedule is updated (requires car plugged in to confirm JuiceBox MCP tools return live data)

### 📋 Backlog
- Add daily scheduling report delivered via notification or log
- Test with real tariff data from Enphase API (vs. mock data in tests)
- Add solar production awareness — prefer charging when production exceeds consumption
- Tune optimizer thresholds based on real-world Arizona TOU schedule
- Add override tool: manual "charge now" regardless of TOU window

### 🔴 Blocked
[Empty]

## ✅ Completed
- **NAS deployment (2026-04-18)**
  - Added SSE transport mode to `server.py` (same pattern as claude-enphase)
  - Created `docker-compose.yml` + updated `Dockerfile` and `requirements.txt`
  - Deployed to `/volume1/docker/enphase-juicebox-coordinator` at port 8767
  - Daily scheduler running: next run 2026-04-19 04:00 America/Phoenix
  - Connected to Claude Desktop at `http://192.168.0.64:8767/sse`
- Coordinator orchestration logic (`coordinator.py`)
- Enphase TOU tariff fetcher (`enphase.py`)
- Peak window optimizer (`optimizer.py`)
- JuiceBox MCP caller (`juicebox_mcp.py`)
- MCP server entry point (`server.py`)
- Full pytest test suite with async mocking
- Dockerfile for NAS deployment
