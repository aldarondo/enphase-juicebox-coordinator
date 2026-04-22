# QA Report — enphase-juicebox-coordinator
**Date:** 2026-04-22

## Summary
| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 9 |
| Minor | 11 |
| **Total** | **20** |

---

## Bugs

### 1. Race condition in surplus monitor state transitions — MAJOR
**Location:** `surplus_monitor.py` lines 70–100; `server.py` ~line 300

`_surplus_state` dict is not protected by a lock. Concurrent writes from the 15-minute polling job and `run_coordinator()` MCP tool could corrupt state or skip state transitions, causing JuiceBox charge amps to oscillate.

**Fix:** Protect `_surplus_state` with `asyncio.Lock` in `server.py`.

---

### 2. Email alert does not surface retry exhaustion clearly — MAJOR
**Location:** `battery_mode.py` lines 145–160

When both battery mode switch attempts fail, the email does not state "ALL RETRIES EXHAUSTED" or clarify that the issue persists until the next 24-hour window. Operators may not recognize the urgency.

**Fix:** Add explicit language to subject/body: "ALERT: Pre-peak mode switch FAILED (retries exhausted)" and "Next retry: tomorrow's peak window."

---

### 3. Tariff parsing does not validate season date wraparound — MINOR
**Location:** `optimizer.py` lines 52–75 (`_active_season()`)

If Enphase API returns malformed seasons with missing `endMonth` for a wraparound season, silent fallback masks the issue.

**Fix:** Add `log.warning(...)` when falling back to legacy matching.

---

### 4. Nominatim geocoding rate-limit not honored in retry loops — MINOR
**Location:** `calendar_check.py` lines 95–110

Multiple geocoding requests in a single calendar check could exceed Nominatim's 1 req/sec limit.

**Fix:** Sleep 1.1 seconds between geocoding requests.

---

## Test Coverage

### 1. Missing integration test for surplus monitor ↔ JuiceBox feedback loop — MAJOR
No test file exists for `surplus_monitor.py`. The full activate/deactivate state machine (2-poll threshold, peak window guard) is untested end-to-end.

**Fix:** Create `tests/test_surplus_integration.py` with 5–8 tests covering activation, deactivation, peak window guard, and overnight mode interaction.

---

### 2. Calendar check iCal parsing not tested against real-world URLs — MAJOR
**Location:** `tests/test_server_calendar.py` lines 1–30

Mock fixtures don't cover recurring events (RRULE), all-day events without DTEND, UTC timezone variations, or non-ASCII location names.

**Fix:** Add 3–4 tests using real-world iCal payloads covering these edge cases.

---

### 3. Battery mode email alert text not validated — MINOR
**Location:** `tests/test_battery_mode.py` lines 176–184

Tests don't verify email recipient matches `ALERT_TO_EMAIL` env var.

---

### 4. Coordinator orchestration not tested for partial failures — MINOR
**Location:** `tests/test_coordinator.py`

No tests for: tariff fetch timeout with cached tariff fallback, or JuiceBox push failing after successful tariff fetch.

---

## Code Quality

### 1. Global mutable state in server.py without clear initialization — MAJOR
**Location:** `server.py` lines ~280–320

Module-level dicts (`_last_result`, `_cached_tariff`, etc.) start as empty `{}`. If an MCP tool is called before the first 04:00 scheduler run, state may be incomplete.

**Fix:** Create `async def _initialize_state()` with safe defaults and call from MCP `initialize()` hook.

---

### 2. Error handling asymmetry: MCP tools swallow exceptions silently — MAJOR
**Location:** `server.py` lines ~400–450

Most MCP tool handlers don't wrap calls in try-catch. Raw exceptions propagate without context.

**Fix:** Wrap each handler: return `{"status": "error", "error": str(e), "timestamp": ...}` on exception.

---

### 3. Async function naming convention not consistent — MINOR
**Location:** `battery_mode.py`, `coordinator.py`, `calendar_check.py`, `server.py`

Some sync utility functions use the `_` prefix inconsistently with async counterparts.

---

### 4. Logging is sparse in critical loops — MINOR
**Location:** `surplus_monitor.py` lines 60–100; `server.py` ~line 750

15-minute polling job doesn't log SOC, surplus watts, or activation decision.

**Fix:** Add structured log line after `compute_charge_amps()` call.

---

## Documentation

### 1. README.md does not document MCP connection failures — MAJOR
No troubleshooting section for when Enphase/JuiceBox/Email MCPs are unreachable.

**Fix:** Add "Troubleshooting" section with curl checks and fallback behavior.

---

### 2. CLAUDE.md session checklist does not mention test verification — MINOR
**Location:** `CLAUDE.md` lines 10–20

Checklist omits `pytest` step.

---

### 3. Email alert subject line references undefined constant — MINOR
**Location:** `battery_mode.py` lines 155–160

Hardcoded `"15:57 pre-peak"` label won't match if tariff peak window changes.

**Fix:** Pass label as parameter from APScheduler job registration.

---

## Organization

### 1. MCP client modules should be in a subpackage — MINOR
`email_mcp.py`, `enphase_mcp.py`, `juicebox_mcp.py` are in the root alongside orchestration logic. A `mcp_clients/` subdirectory would clarify the structure.

---

### 2. Test file naming convention inconsistent — MINOR
`test_server_battery_mode_tools.py` vs `test_battery_mode.py` creates confusion about scope.

---

## Security

### 1. Email recipient default is hardcoded to operator email — MAJOR
**Location:** `email_mcp.py` line 19

`ALERT_TO_EMAIL` defaults to `charles.aldarondo@gmail.com` if env var not set. Email exposed in source.

**Fix:** Default to `None` and skip email if not set, or raise `RuntimeError` if required.

---

### 2. Nominatim geocoding does not validate location responses — MINOR
**Location:** `calendar_check.py` lines 85–95

Malformed or attacker-controlled location strings could return invalid coordinates that bypass distance thresholds.

**Fix:** Validate lat/lon ranges after geocoding response.

---

### 3. Tariff data cached but not validated for expiration — MINOR
**Location:** `server.py` ~line 305

Invalid tariff structures (e.g., `startMonth: 13`) served from cache without validation.

**Fix:** Add `validate_tariff()` in `optimizer.py` after each fetch.

---

## Top 3 Priority Items

1. **Security #1 (Major):** Remove hardcoded `charles.aldarondo@gmail.com` from `email_mcp.py` — privacy risk if repo is ever made public.
2. **Bugs #1 (Major):** Add `asyncio.Lock` to `_surplus_state` in `server.py` — race condition could cause erratic charging behavior in production.
3. **Code Quality #2 (Major):** Wrap MCP tool handlers in try-catch in `server.py` — silent exception propagation makes production debugging very hard.
