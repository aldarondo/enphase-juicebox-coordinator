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
| `server.py` | MCP server, APScheduler jobs (04:00 daily run, 21:00 calendar check, 15-min surplus poll) |
| `juicebox_mcp.py` | JuiceBox MCP tool caller (claude-juicebox at `:3001/sse`) |
| `enphase_mcp.py` | Enphase MCP tool caller (claude-enphase at `:8766/sse`) |
| `Dockerfile` | NAS deployment container |
| `docker-compose.yml` | NAS compose config (port 8767) |

## MCP Tools Exposed

- `run_coordinator` — trigger an immediate tariff fetch + JuiceBox schedule update
- `get_status` — current state: schedule, overnight flag, calendar result, last run time
- `get_surplus_status` — surplus monitor state: SOC, production, consumption, active/inactive
- `charge_now` — push an immediate charging window (optional `hours` param; reverts at next 04:00 run)
- `get_weekly_report` — last Sunday's charging report

## Scheduled Jobs

| Time | Job |
|---|---|
| 04:00 daily (Arizona) | Full coordinator run — fetch tariff, compute schedule, program JuiceBox. Resets overnight flag to disabled (surplus-only). |
| 21:00 daily (Arizona) | Calendar check — reads Google Calendar iCal feeds, geocodes next-day events, enables overnight TOU if driving distance > 50 miles |
| Every 15 min | Surplus monitor — activates/deactivates JuiceBox based on SOC + solar surplus |

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
pytest                  # 57 tests
python -m server        # run locally (stdio mode)
```

Requires `claude-enphase` at `:8766/sse` and `claude-juicebox` at `:3001/sse` for full integration.
