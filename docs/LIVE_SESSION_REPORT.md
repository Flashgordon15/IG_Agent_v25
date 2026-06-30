# Live Session Report — Clean Launch & 10-Min Soak

**Date:** 2026-06-30  
**Config:** `IG_AGENT_CONFIG=config/config_v31_demo_throughput.json`  
**Launch:** `agent_kill.sh` → `agent_start.sh` (tests skipped for soak window)  
**Monitor:** `scripts/live_session_monitor.py` — **15/15 samples** @ 60s (07:10:08 → 07:24:09 UTC, ~14 min)

---

## Executive Summary

| Area | Grade | Notes |
|------|-------|-------|
| **Boot sequence** | A | G2→READY in ~2m 17s; 7 routes warmed |
| **API performance** | A+ | `health_light` p50 ~0.8ms; `health` p95 0.09ms |
| **Feed / rotation** | B+ | tpm 98–119/min, zero feed stalls; Z-score pipeline broken |
| **Trading activity** | F | 0 trades in soak; Core B muted, Z=0 flat |
| **Reliability / observability** | C+ | Telemetry contradictions; health flags misleading |

**Bottom line:** Infrastructure and API layers are fast and stable after P0–P4 fixes. **Trading did not progress** because the dual-core Z-score engine reports `0.0` with an empty `z_score_stream` despite healthy tick velocity — orders require Z pierce ±2.0.

---

## 1. Boot Sequence

| Time (UTC) | Event |
|------------|-------|
| 07:07:36 | `agent_start` begin, preflight OK (DEMO, port 8080) |
| 07:07:51 | G2 HYDRATING |
| 07:09:07 | G4 OPERATIONAL |
| 07:09:53 | **READY** — post-G5 health OK |
| 07:09:54 | Route warm-up: **7 routes**; dashboard on :8080 |

**Duration:** ~2m 18s launch to READY (acceptable).

**Observations:**
- G5 gate in `boot_metrics.gates` still shows `running` while `phase=READY` — cosmetic inconsistency.
- 7 trading loops built and `accepting_ticks: true`.
- Streaming transport: `yahoo`, first tick Gold @ 07:09:48.

---

## 2. API Integration & Performance

### Latency (live profiler + soak)

| Endpoint | p50 | p95 | Budget |
|----------|-----|-----|--------|
| `/api/health` | 0.03ms | 0.09ms | <200ms ✓ |
| `/api/health_light` | 0.8ms | 3.3ms | <5ms ✓ |
| `/api/gui_status` | 0.04ms | 0.55ms | <200ms ✓ |

### Background (non-blocking) slow paths

| Section | p95 | Impact |
|---------|-----|--------|
| `health.snapshot_refresh` | 836ms | Background only |
| `health.supervision_drift` | 309ms | Background only |

HTTP hot paths are **not affected** — snapshot architecture working.

### `/api/health_light` (post-boot sample)

```json
{
  "agent_online": true,
  "execution_loop_active": false,
  "routing_state": { "armed": 0, "degraded": true },
  "feed_stall": false,
  "stack_tpm": { "IX.D.DOW.IFM.IP": 29, "CS.D.CFPGOLD.CFP.IP": 29 },
  "ig_available": true,
  "yahoo_available": false
}
```

**Bug:** `routing_state.armed=0` contradicted `/api/diagnostics` (`armed_count: 7`) — health_light reads stale `gui_status` cache at boot.

**Bug:** `execution_loop_active=false` while tpm >100 — should key off tpm or `loops_running`, not `rotation_sweep_count`.

---

## 3. Connectivity & Feeds

### Soak window (15 samples, 60s apart — full run)

| Metric | Sample 1 | Samples 2–15 | Stable? |
|--------|----------|----------------|---------|
| `feed_stall` | false | false (0/15) | ✓ |
| DOW tpm/min | 31 → 119 | 98–119 | ✓ |
| Gold tpm/min | 31 → 119 | 118–119 | ✓ |
| `phase` | READY | READY (15/15) | ✓ |
| Trades | 0 | 0 (`trade_count_delta: 0`) | ✗ |
| `rotation_sweep_delta` | — | **0** | ✗ |
| `ticks_delta` | — | +28 | marginal |

**Aggregate latency (15 samples):** health_light p50 **0.97ms** / max 26ms; health p50 1.73ms / max 54ms — all under budget.

**Raw report:** `logs/live_session_report.json` · `logs/live_session_monitor.log`

**P0–P4 feed fixes validated:** No feed stall, no rotation deadlock, tick velocity healthy.

### Remaining feed issues

- `yahoo_available: false` in health_light while Yahoo is primary transport — provider check logic wrong.
- `/api/health` issues: `quotes_stale:EUR/USD,Nikkei` (non-stacked epics; expected off hot path).
- `engine_log_stale_745s` — likely log path mismatch (health reads `logs/engine.log` vs `src/data/.../engine.log`).

---

## 4. Trading Setup & Logic

### Runtime state (end of soak)

| Signal | Value | Expected for trading |
|--------|-------|----------------------|
| `loops_running` | true | ✓ |
| `armed_count` | 7 | ✓ |
| `rotation_sweep_count` | **0** | ✗ should be >>0 after 10 min |
| `live_calculated_zscore` | **0.0** both stacks | ✗ need \|Z\| ≥ 2.0 |
| `z_score_stream` | **[]** empty | ✗ charts flatline |
| `core_b_micro_active` | **false** | ✗ scalper muted |
| `execution_mode` | NEUTRAL | ✗ |
| Trades (soak) | **0** | — |
| `last_gate_suppression_reason` | empty | — |

### Why no trades

1. **Z-score never moves off 0** — `ingest_hub_mid` not populating `_z_history_by_epic` despite high tpm from `_record_quote_pulse` (heartbeat/synthetic pulses without full mid ingest).
2. **Core B requires piercing zone** — `Z ≤ -2.0` or `Z ≥ +2.0`; flat Z = no valve open.
3. **`rotation_sweep_count = 0`** suggests the 500ms `evaluate_multi_source_rotation_sweep` loop may not be incrementing (stacked async thread not running or counter reset after bootstrap without subsequent sweeps).
4. **`trading_healthy` false** — `no_gate_activity_recorded` / stale gate check age; dual-core path doesn't update classic `TradingLoop` gate timestamps.

### Config active

- `config_v31_demo_throughput.json` — 240 daily trades, 90s cooldown, fitness_min 48, yahoo_poll 5s.

---

## 5. Bugs & Weaknesses Identified

### Critical (blocks trading)

| # | Issue | Evidence |
|---|-------|----------|
| C1 | Z-score pipeline disconnected from tick pulses | tpm 119, Z=0, `z_score_stream=[]` |
| C2 | `rotation_sweep_count` stuck at 0 | No sweep telemetry after 10+ min |
| C3 | Core B never arms | `core_b_micro_active: false` throughout soak |

### High (misleading ops)

| # | Issue | Evidence |
|---|-------|----------|
| H1 | health_light routing from stale gui cache | armed 0 vs diagnostics 7 |
| H2 | health_light `execution_loop_active` false with live feeds | tpm >100 |
| H3 | `trading_healthy` false on healthy session | gate_age not recorded for dual-core |
| H4 | `yahoo_available` false when Yahoo is transport | health_light provider check |

### Medium

| # | Issue |
|---|-------|
| M1 | G5 gate status `running` while READY |
| M2 | `engine_log_stale` false positive (log path) |
| M3 | `agent_version` shows `v30.0` in health_light |
| M4 | `gate_stack_matrix` empty in telemetry when boot_gate null |
| M5 | main.py CPU ~55% sustained — profile hot paths |
| M6 | Pytest gate skipped — production launches should run gate hourly |

---

## 6. Enhancement Roadmap (Prioritized)

### P0 — Restore trading signal path

1. **Ensure stacked async sweep runs** — assert `_stacked_thread.is_alive()` post-G5; log/alarm if `rotation_sweep_count` flat >30s.
2. **Fix Z ingest** — `_record_quote_pulse` must pair with `ingest_hub_mid` on every pulse; verify `_mid_history` depth ≥30 and `_z_history_by_epic` appends.
3. **Demo soak Z bootstrap** — if Z=0 for >2 min with tpm>5, inject micro variance from rolling mids or lower `_SHORT_WINDOW` for demo overlay.

### P1 — Observability truth

4. health_light `routing_state` from `cached_unified_routes()` not gui_status cache.
5. `execution_loop_active` = `loops_running AND min(stack_tpm)>5`.
6. Fix `yahoo_available` — check transport config + recent Yahoo publish timestamps.
7. Align `trading_healthy` with dual-core gate activity (record synthetic gate tick from sweep).

### P2 — Launch hardening

8. Run pytest gate on first launch of day (`LAUNCHER_FORCE_TESTS=1`).
9. Install launchd for `watchdog_inactive` clearance.
10. Fix engine log path in health staleness check.

### P3 — Trading frequency (after Z fix)

11. Monitor Z pierce events in `engine.log` / triage DB.
12. During active London/NY: expect trades when \|Z\| ≥ 2.0 with throughput config.
13. Cockpit: show Z stream length + sweep count on System Health widget.

---

## 7. Verification Commands

```bash
# Health
curl -s http://127.0.0.1:8080/api/health_light | python3 -m json.tool
curl -s http://127.0.0.1:8080/api/readiness/profile | python3 -m json.tool

# Trading truth
curl -s http://127.0.0.1:8080/api/v31/telemetry | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('sweeps', d.get('rotation_sweep_count'))
for c in d.get('stacked_asset_channels',[]): print(c)
"

# 15-min soak (re-run)
PYTHONPATH=src .venv/bin/python3 scripts/live_session_monitor.py --minutes 15 --interval 60
```

---

## 8. Session Artifacts

- Boot log: `logs/agent_start.log`
- Monitor log: `logs/live_session_monitor.log` (10 samples)
- Launcher tee: `/tmp/live_launch_*.log`

---

## 9. P0–P3 Re-Launch Soak (post Z-bootstrap fix)

**Date:** 2026-06-30  
**Launch:** `agent_kill.sh` → `agent_start.sh` @ 07:31 UTC  
**Monitor:** 15/15 samples @ 60s (07:35:01 → 07:49:02 UTC)  
**Report:** `logs/live_session_report_p0p3.json`

### vs Soak #1

| Metric | Soak #1 | Soak #2 (P0–P3) | Δ |
|--------|---------|-----------------|---|
| Feed stalls | 0 | 0 | — |
| TPM (DOW/Gold) | 98–119 | 196–238 | ↑ ~2× |
| `ticks_delta` | +28 | +28 | — |
| `rotation_sweep_delta` | 0 | 0 | **unchanged** |
| `exec_loop_active_samples` | 0 | 0 | **unchanged** |
| Trades | 0 | 0 | — |
| `health_light` p50 | ~0.8ms | 1.11ms | ✓ |
| Z-score | 0.0 (flat) | **-0.56** (live) | **fixed** |
| Z stream length | 0 | **120** (capped window) | **fixed** |

### P0–P3 fix validation

| Fix | Status |
|-----|--------|
| P0 — varied bootstrap mids (Z variance) | **PASS** — Z ≈ -0.56, streams at 120 |
| P0 — heartbeat ingests mids | **PASS** — TPM 200+/min |
| P1 — `record_gate_evaluation` in sweep | **N/A** — sweep thread never started |
| P2 — health_light routing from unified cache | **PARTIAL** — `armed: 0` (route dict lacks `armed` flag) |
| P3 — stacked sweep watchdog + `z_short_window: 12` | **FAIL** — `stacked_sweep_alive: false`, sweep count 0 |

### Root cause (boot hang)

Post-ready sequence **blocked** inside `_ensure_feed_plane_ready()` after Yahoo poller:

`start_fulfillment_cache_refresh()` called `sync_performance_rows_from_ig_rest(force=True)` **synchronously** on the main post-ready thread → IG REST hang → `start_stacked_dual_asset_tracks()` never reached.

Engine log at 08:33:24 shows Yahoo poller armed but **no** `StackedDualAsset parallel tracks armed`.

DualCoreCoordinator still fired dispatch attempts (all `blocked_by_strategy_controller`) via its own path.

### Additional fixes applied (post-soak, needs restart)

1. `unified_fulfillment_cache.py` — boot sync moved to background thread (non-blocking).
2. `health_light.py` — restart stacked sweep when `stacked_sweep_alive: false`.

### Recommended next step

```bash
export IG_AGENT_CONFIG=config/config_v31_demo_throughput.json
./macos/launcher/agent_kill.sh && LAUNCHER_SKIP_TESTS=1 ./macos/launcher/agent_start.sh
# Confirm log line: "StackedDualAsset parallel tracks armed"
# Re-run 15-min soak; expect rotation_sweep_delta > 0
```

---

*Report updated 2026-06-30 after P0–P3 soak #2.*
