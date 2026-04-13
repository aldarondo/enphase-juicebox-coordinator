# enphase-juicebox-coordinator — Roadmap

## Current Milestone
End-to-end deployment — coordinator running on NAS, auto-scheduling EV charging around TOU peak hours

### 🔨 In Progress
[Empty]

### 🟢 Ready (Next Up)
- Deploy claude-juicebox (dependency) — coordinator cannot run until JuiceBox MCP is live
- Deploy coordinator Docker container to Synology NAS alongside claude-enphase

### 📋 Backlog
- Add daily scheduling report delivered via notification or log
- Test with real tariff data from Enphase API (vs. mock data in tests)
- Add solar production awareness — prefer charging when production exceeds consumption
- Tune optimizer thresholds based on real-world Arizona TOU schedule
- Add override tool: manual "charge now" regardless of TOU window

### 🔴 Blocked
- `claude-juicebox` MCP server must be implemented and deployed first

## ✅ Completed
- Coordinator orchestration logic (`coordinator.py`)
- Enphase TOU tariff fetcher (`enphase.py`)
- Peak window optimizer (`optimizer.py`)
- JuiceBox MCP caller (`juicebox_mcp.py`)
- MCP server entry point (`server.py`)
- Full pytest test suite with async mocking
- Dockerfile for NAS deployment
