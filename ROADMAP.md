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

### 🔁 Ongoing / Recurring
- **Daily coordinator log review** — Each day, pull the previous 24h of logs from the NAS (`python skills/synology.py ssh "sudo docker logs enphase-juicebox-coordinator --since 24h 2>&1 | tail -500" --sudo`) and audit:
  - Did the 04:00 tariff fetch + schedule push succeed? (`status=ok`, `juicebox_ok=True`)
  - Did the 21:00 calendar check + immediate JuiceBox push run cleanly?
  - Did pre-peak (Savings → Self-Consumption) and post-peak (Self-Consumption → Savings) battery mode switches fire on weekdays at the tariff-derived times, with confirmation reads matching the target?
  - Did the surplus solar monitor activate when SOC ≥ 95% and production > consumption, and revert correctly?
  - Any tracebacks, retries, email failure alerts, or tariff parse fallbacks?
  - Compare actual JuiceBox charging windows vs. expected (avoid 16:00–19:00 peak; use super off-peak 10:00–15:00 in winter / overnight when calendar enables it).

  For each anomaly, file a fix on this roadmap and ship it the same day so the next 24h cycle improves. Sunday weekly report (already emailed at 07:17 AZ) is the rollup; this task is the daily granular check.

## ✅ Completed
- **Durable rotating file logs (2026-04-22)** — Added `RotatingFileHandler` alongside stdout: 10 MB per file, 10 backups (~100 MB max). Logs mount to `/volume1/docker/enphase-juicebox-coordinator/logs/coordinator.log` on the NAS so they survive container restarts and image rebuilds. `LOG_DIR` env var overrides the path.
- **QA review of battery mode switching PR + fixes applied (2026-04-21)** — Two issues caught in QA and resolved before merge: (1) mode-switch cron jobs were missing `timezone=ARIZONA` on initial registration — fixed by initializing `AsyncIOScheduler(timezone=ARIZONA)` at the scheduler level so all jobs inherit it; (2) `switch_battery_mode` and `get_battery_mode_status` MCP tool handlers had no test coverage — addressed via new `tests/test_server_battery_mode_tools.py`. 148 tests total.
- **Calendar check pushes to JuiceBox immediately at 21:00 (2026-04-22)** — Previously `_nightly_calendar_check` only flipped `_overnight_charging.enabled`, with the actual JuiceBox push deferred to the 04:00 coordinator run — but 04:00 is too late to "charge overnight" before a morning commute. Calendar check now calls `coordinator.run()` (enabled) or `juicebox_mcp.set_charging_schedule([])` (disabled) immediately after setting the flag. 04:00 run is now a safety-net / idempotent re-push. 13 new tests (110 total).
- **Mode switches are weekday-only and tariff-derived (2026-04-22)** — Cron constrained to `day_of_week='mon-fri'` (APS peak is weekday-only). New `_peak_switch_times(tariff)` derives switch times from `optimizer._find_peak_weekday_hours`: pre = peak_start - 3 min, post = peak_end + 2 min. `_reschedule_battery_mode_jobs()` called after every 04:00 tariff refresh so peak-window changes propagate automatically. Falls back to APS default 16:00–19:00 → 15:57/19:02 if tariff unparseable. Runtime gate: jobs skip if tariff shows no weekday peak.
- **Enphase battery mode switching at peak boundaries (2026-04-22)** — New `battery_mode.py` toggles the Enphase profile twice daily: pre-peak Savings → Self-Consumption (solar covers load during 16:00–19:00 peak instead of being exported at low rate while battery discharges) and post-peak Self-Consumption → Savings (restore TOU-aware discharge for evening). Each switch reads current mode, skips if already at target, sets + confirms via `enphase_set_battery_mode`, retries once after 10s on failure. Retry failure emails Charles via new `email_mcp.py` → `claude-email` MCP with failure consequence. New MCP tools `switch_battery_mode` (manual) and `get_battery_mode_status`. 31 new tests.
- **Charging priority model + surplus-first architecture (2026-04-21)** — Flipped default overnight charging to disabled (surplus solar is primary mode). Calendar check at 21:00 enables overnight TOU only when a long trip is detected next day. `_revert_to_tou_schedule()` fixed falsy-empty-list bug (now uses `"schedule" in _last_result`). When overnight is disabled, JuiceBox schedule is set to `[]` (empty); surplus monitor is sole charging trigger.
- **Super off-peak rate-tier awareness (2026-04-21)** — `optimizer.py` gains `_find_daytime_window()` which detects uncovered minute gaps between 10:00 and peak_start. Winter APS tariff → 10:00–15:00 super off-peak; summer (no gap) → fallback 10:00–peak_start. `compute_schedule()` accepts `overnight_enabled` param and uses daytime window when disabled. 57 tests passing.
- **Cloudflare Tunnel SSH deploy fix (2026-04-21)** — GitHub Actions SSH through Cloudflare Tunnel now uses a service token (`non_identity` policy). Previously the SSH-type Access app rejected non-interactive cloudflared even with "bypass everyone" policy. Created service token via new `scripts/create-access-service-token.mjs` in claude-cloudflare; stored `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` as GitHub Actions secrets. Deploy pipeline working (run #24727724620).
- **GOOGLE_ICAL_URLS persistence (2026-04-21)** — iCal URLs stored as GitHub Actions secret `GOOGLE_ICAL_URLS`. `build.yml` base64-encodes and writes `.env` to NAS on every deploy, so the file survives container restarts. Calendar check now stays disabled (surplus-only) if iCal not configured, rather than enabling overnight charging on failure.
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

- **Verify stormAlert response key for Storm Guard detection** — `enphase_get_storm_guard` tool was added to claude-enphase and the coordinator checks it before every battery mode switch. However, the exact JSON key in the Enphase `stormAlert` API response is unknown — the handler tries `alertActive`, `active`, and `stormAlert` as candidates. **Blocked until next Storm Guard activation**: call `enphase_get_storm_guard` when a storm alert is active, inspect the `raw` field in the response, and confirm or fix the key check in `claude-enphase/server.py` (`call_tool` handler for `enphase_get_storm_guard`). One-line fix once the real key is known.
