# enphase-juicebox-coordinator

Smart coordinator that programs the JuiceBox EV charger based on Enphase solar production and APS time-of-use tariff rates.

## Charging Priority Model

1. **Long trip tomorrow** (detected via Google Calendar at 21:00) → enable overnight TOU charging (off-peak window only, avoids 16:00–19:00 peak)
2. **Surplus solar** (continuous 15-min poll) → activate JuiceBox when battery SOC ≥ 95% AND solar exceeds home load by ≥ 400W
3. **Default** → car does not charge; house battery absorbs all surplus

The surplus solar monitor is the primary charging mechanism. Overnight TOU charging is an exception triggered only by calendar-detected long trips.

## APS R-3 TOU Rate Structure

| Period | Window | Rate (winter) |
|---|---|---|
| Super off-peak | 10:00–14:59 weekdays | $0.036/kWh |
| Mid-peak | 00:00–09:59, 15:00–15:59 weekdays | $0.049–0.061/kWh |
| Peak | 16:00–18:59 weekdays | $0.101/kWh |
| Off-peak | 19:00–23:59 weekdays, all weekend | (cheapest) |

Summer has no super off-peak period — optimizer falls back to full 10:00–16:00 window.

## Key Files

| File | Purpose |
|---|---|
| `coordinator.py` | Main orchestration: fetches tariff, runs optimizer, programs JuiceBox |
| `optimizer.py` | TOU peak detection, daytime window calculation, schedule generation |
| `battery_mode.py` | Enphase battery-profile switch logic (read → skip-if-target → set → confirm → retry → email on failure) |
| `server.py` | MCP server, APScheduler jobs (04:00 daily run, 21:00 calendar check, 15-min surplus poll, 15:57/19:02 mode switches) |
| `juicebox_mcp.py` | JuiceBox MCP tool caller (claude-juicebox at `:3001/sse`) |
| `enphase_mcp.py` | Enphase MCP tool caller (claude-enphase at `:8766/sse`) |
| `email_mcp.py` | claude-email MCP tool caller (failure alerts) |
| `Dockerfile` | NAS deployment container |
| `docker-compose.yml` | NAS compose config (port 8767) |

## MCP Tools Exposed

- `run_coordinator` — trigger an immediate tariff fetch + JuiceBox schedule update
- `get_status` — current state: schedule, overnight flag, calendar result, last run time
- `get_overnight_mode` / `set_overnight_mode` — inspect or manually flip the overnight TOU flag. `set_overnight_mode` pushes to the JuiceBox immediately (TOU schedule or clear) — same path the 21:00 calendar check uses
- `get_surplus_status` — surplus monitor state: SOC, production, consumption, active/inactive
- `charge_now` — push an immediate charging window (optional `hours` param; reverts at next 04:00 run)
- `run_calendar_check` — trigger the 21:00 calendar check on demand (also pushes to JuiceBox)
- `get_weekly_report` — last Sunday's charging report
- `switch_battery_mode` — manually switch Enphase battery profile (`self-consumption` or `savings`); same path the scheduler uses at 15:57 / 19:02
- `get_battery_mode_status` — result of the most recent battery-mode switch (target, applied, attempts, errors)

## Scheduled Jobs

| Time | Job |
|---|---|
| 21:00 daily (Arizona) | Calendar check — reads Google Calendar iCal feeds, geocodes next-day events. If driving distance > threshold, enables overnight TOU **and immediately pushes the TOU schedule to JuiceBox** so the car can start charging at plug-in time. If no trip, immediately clears the schedule to `[]` (surplus-only). |
| 04:00 daily (Arizona) | Safety-net / idempotent re-push of whatever the 21:00 check decided. Also refreshes cached tariff and reschedules the mode-switch jobs against the live peak window. Resets overnight flag. |
| 15:57 **weekdays** (Arizona, tariff-derived) | Pre-peak battery mode switch: Savings → Self-Consumption (solar covers load during the peak instead of being exported at low rate) |
| 19:02 **weekdays** (Arizona, tariff-derived) | Post-peak battery mode switch: Self-Consumption → Savings (restore TOU-aware discharge for the evening) |
| Every 15 min | Surplus monitor — activates/deactivates JuiceBox based on SOC + solar surplus |

## Logs

All scheduler actions and tool calls are logged with timestamps to:

- **stdout** — `docker logs enphase-juicebox-coordinator -f`
- **Persistent file** — `/volume1/docker/enphase-juicebox-coordinator/logs/coordinator.log` (survives container restarts)

Rotating: 10 MB per file, 10 backups (~100 MB max). Override path with `LOG_DIR` env var.

Structured prefixes make grep easy:

| Prefix | Source |
|---|---|
| `[scheduler]` | APScheduler jobs (04:00 daily, mode switches) |
| `[surplus_monitor]` | 15-min surplus poll activations/reverts |
| `[overnight]` | Overnight mode push to JuiceBox |
| `[calendar_check]` | 21:00 Google Calendar check |
| `Tool:` | Manual MCP tool invocations |

## Deployment

Images build automatically on push to `main` via GitHub Actions → GHCR → NAS pull.

### Required GitHub Secrets

| Secret | Purpose |
|---|---|
| `NAS_SSH_PASSWORD` | NAS sudo password for SSH deploy |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token ID |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret |
| `GOOGLE_ICAL_URLS` | Comma-separated Google Calendar iCal feed URLs |

`GOOGLE_ICAL_URLS` is written to `/volume1/docker/enphase-juicebox-coordinator/.env` on every deploy.

### Adding Calendar Feeds

1. In Google Calendar → Settings → [calendar] → "Secret address in iCal format"
2. Copy the `.ics` URL
3. `gh secret set GOOGLE_ICAL_URLS --repo aldarondo/enphase-juicebox-coordinator --body "url1,url2"`
4. Push any change to trigger a deploy (or use workflow_dispatch)

## Development

```bash
pip install -r requirements.txt
pytest                  # run test suite
python -m server        # run locally (stdio mode)
```

Requires `claude-enphase` at `:8766/sse` and `claude-juicebox` at `:3001/sse` for full integration. Optional: `claude-email` at `:8768/sse` for failure alerts on scheduled battery-mode switches.

## Troubleshooting

**Coordinator reports "error" status:**
- Check Enphase MCP: `curl http://<NAS-IP>:8766/sse` (connection should open, not immediately error)
- Check JuiceBox MCP: `curl http://<NAS-IP>:3001/sse` (same)
- Check container logs: `docker logs enphase-juicebox-coordinator --tail 50`
- Check persistent logs: `tail -f /volume1/docker/enphase-juicebox-coordinator/logs/coordinator.log`

**Battery mode alerts not sending:**
- Verify `ALERT_TO_EMAIL` env var is set (no default — alerts are silently skipped if unset)
- Verify `EMAIL_MCP_URL` is set (e.g. `http://172.18.0.1:8768/sse`) and `brian-email` is running
- Verify `EMAIL_MCP_API_KEY` matches `MCP_API_KEY` in `brian-email/.env` — a mismatch returns 401 and silently fails
- Check container logs for `[battery_mode] Failed to send failure alert email`

**Surplus monitor not activating:**
- Call `get_surplus_status` to check current SOC, production, and `surplus_poll_count`
- Activation requires `ACTIVATION_POLLS=2` consecutive readings above threshold
- Charging is blocked during the peak window (16:00–19:00 + 15-min buffer each side)
- Requires `battery_soc >= 95%` AND `production − consumption >= 400W`

**JuiceBox schedule not updating after overnight mode change:**
- `set_overnight_mode` pushes to JuiceBox immediately — check its response for `juicebox_ok`
- If push failed, the 04:00 daily run retries automatically
- Force a retry: call `run_coordinator` (if enabled) or `set_overnight_mode` again

## Enphase Battery Mode Switching

The home Enphase system sits in **Savings Mode** against an APS TOU tariff. During the 16:00–19:00 peak window, Savings Mode discharges the battery aggressively regardless of live solar production — Phoenix solar is still generating meaningfully at that hour, so the system ends up simultaneously draining the battery AND exporting surplus solar at the low export rate. Enphase has no setting to fix this.

The coordinator works around it by toggling the battery profile at the peak boundaries, **weekdays only** (APS peak is weekday-only):

| Time | Action | Effect |
|---|---|---|
| `peak_start − 3 min` (default 15:57) | Savings → Self-Consumption | Solar covers home load first; battery only fills the gap; excess solar charges the battery instead of exporting at low rate. |
| `peak_end + 2 min` (default 19:02) | Self-Consumption → Savings | Solar is gone; restore TOU-aware discharge for the evening hours. |

The switch times are derived from the tariff's peak window (via `optimizer._find_peak_weekday_hours`). The 04:00 daily coordinator run refreshes the cached tariff and reschedules the mode-switch jobs — if APS ever shifts peak to, say, 15:00–18:00, the jobs automatically move to 14:57 / 18:02. If the tariff can't be parsed, the jobs default to APS's historical 15:57 / 19:02. At job run-time, if the tariff has no weekday peak window at all (unlikely), the switch is skipped with a log.

Each switch reads the current mode first and skips if it's already on target (manual correction). On API failure, the switch retries once after 10s. If the retry also fails, an alert email is sent to `ALERT_TO_EMAIL` via the `claude-email` MCP with the failure consequence spelled out. Successful switches are silent.
