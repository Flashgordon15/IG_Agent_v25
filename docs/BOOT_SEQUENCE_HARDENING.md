# Boot Sequence Hardening — Cold Start to Active Trading

Assessment date: 2026-06-30. Scope: launcher scripts, gates G1–G5, post-ready services, health endpoints, dual-core stacked sweep.

## Pre-audit checklist (run before any kill/restart)

| Check | How |
|-------|-----|
| Market sessions closed? | No open positions; avoid restart during London/US/Gold active hours unless operator-approved |
| Watchdog hold active? | `PYTHONPATH=src .venv/bin/python3 -c "from system.shutdown_cleanup import manual_stop_active; print(manual_stop_active())"` |
| Active PIDs clean? | `lsof -iTCP:8080 -sTCP:LISTEN` and `pgrep -fl main.py` |

**2026-06-30 audit:** positions=0, manual_stop=False, agent PID 14306 on :8080. Live cold-start **skipped** — European/London session open (~08:54 BST); restart deferred per trading-session law.

## Boot phase map

```
launcher agent_kill.sh
  → mark_manual_stop (blocks launchd)
  → TERM/KILL families, free :8080, purge bytecode/locks

launcher agent_start.sh
  → [PREFLIGHT] session_lock
  → [TEST] isolated pytest gate (skip: LAUNCHER_SKIP_TESTS=1)
  → [AGENT] daemon_supervisor → main.py
  → wait_g5 (poll /api/health, up to 6 min)
  → wait_interpreter_stable
  → wait_post_ready_execution (poll /api/health_light, up to 90s)  ← NEW
  → [WARMUP] unified route cache
  → [GUI] dashboard dist / vite

main.py (non-blocking boot default)
  → bind :8080 fast
  → background G1→G5 via gate coordinator
  → Gate5: unpause loops, set_ready, start_post_ready_services

post_ready_services (sync thread, non-blocking children)
  → route cache warm (background thread)
  → KernelInterceptor, schedulers, health_light refresher
  → DualCoreCoordinator
  → _ensure_feed_plane_ready: Yahoo poller → fulfillment cache (thread) → feed guardian
  → start_stacked_dual_asset_tracks: prewarm + bootstrap + 500ms sweep thread
  → SocketHeartbeat, VirtualStop, ledger hydration (background)
```

### Expected log lines (post-ready order)

1. `post-ready: unified route cache warm-up scheduled (background)`
2. `post-ready: DualCoreCoordinator ENGINE_B_MICRO_SCALPER armed`
3. `post-ready: Yahoo poller armed … ok (Nms)`
4. `post-ready: Fulfillment cache refresh started (background SHM) ok (Nms)`
5. `post-ready: Agent feed guardian started ok (Nms)`
6. `MultiSourceRotation: 500ms sweep armed universe=…`
7. `post-ready: StackedDualAsset parallel tracks armed`
8. `post-ready: SocketHeartbeat validator armed`
9. `post-ready: HealthLight 1s refresher started`

### Timing budgets (typical DEMO)

| Phase | Budget |
|-------|--------|
| Port bind (G1 partial) | < 5s |
| G2–G4 (background) | 15–90s |
| G5 READY flip | < 120s from process start |
| Post-ready feed plane | < 500ms (no universe REST scan) |
| Stacked sweep first tick | < 2s after step 6 |
| health_light `execution_loop_active` | true within 5–30s of sweep start |
| Launcher post-ready wait | up to 90s (`LAUNCHER_POST_READY_TIMEOUT_SEC`) |

## Issues found and fixes

| ID | Severity | Issue | Fix |
|----|----------|-------|-----|
| BSH-1 | P1 | `health_light.routing_state.armed` always 0 — routes use `execution_path`, not `armed` | `health_light.py`: count `execution_path != NONE` |
| BSH-2 | P1 | `bootstrap_multi_source_rotation_stack` in `_ensure_feed_plane_ready` blocked post-ready (sync Yahoo/REST per epic) | Removed from feed plane; only `start_stacked_dual_asset_tracks` bootstraps |
| BSH-3 | P2 | Duplicate `start_agent_feed_guardian()` in post_ready | Removed early call; guardian only in feed plane |
| BSH-4 | P2 | Post-ready steps lacked timing telemetry | `_log_step_outcome()` with ms per step |
| BSH-5 | P2 | Launcher stopped at G5 — no execution-plane confirmation | `wait_post_ready_execution()` polls health_light |
| BSH-6 | — | `start_fulfillment_cache_refresh()` blocking | **Already fixed** — daemon thread with `boot_force` |

## Verification checklist

```bash
# Unit tests
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_health_light.py tests/test_unified_routing_boot_warmup.py -q

# Live agent (no restart)
curl -s http://127.0.0.1:8080/api/health_light | python3 -m json.tool
# Expect: stacked_sweep_alive=true, rotation_sweep_count increasing,
#         execution_loop_active=true, routing_state.armed > 0

# Cold start (flat + sessions closed + operator approval)
export IG_AGENT_CONFIG=config/config_v31_demo_throughput.json
./macos/launcher/agent_kill.sh
LAUNCHER_SKIP_TESTS=1 ./macos/launcher/agent_start.sh
```

### health_light fields to watch during boot

- `stacked_sweep_alive` — stacked thread running
- `rotation_sweep_count` — must increase every ~500ms
- `execution_loop_active` — sweep advancing or tpm ≥ 5
- `boot_grace_active` — true for first 180s after stacked start
- `routing_state.armed` — routes with non-NONE execution_path
- `z_stream_lengths` — should grow toward 120 per stack epic

## Remaining recommendations

1. **Cold-start soak** — run full kill/start when sessions flat and operator approves; capture `logs/agent_start.log` timings.
2. **Boot progress endpoint** — optional `/api/boot_progress` exposing gate phase + post-ready step timestamps from SystemState.
3. **agent_kill manual_stop** — main.py clears on boot; document that launcher kill intentionally engages hold until agent binds.
4. **Route cache at G5** — `warm_unified_execution_route_cache` in launcher may duplicate post-ready warm; acceptable but adds ~1s post-G5.

## Follow-up fixes (post-audit)

| Fix | File | Issue |
|-----|------|-------|
| Async rotation bootstrap | `dual_core_execution.py` | Watchdog restart blocked on sync `bootstrap_multi_source_rotation_stack()` — sweep froze at 524 |
| Skip staticmethod wrap | `kernel_interceptor.py` | `TradeManager.confidence_band` TypeError spam every tick |

Deploy requires restart when sessions flat.

## Boot pipeline orchestrator (stages A–G)

New modules:
- `src/system/boot/boot_orchestrator.py` — stage/subsystem tracking, `trade_ready` contract
- `src/system/boot/subsystem_healer.py` — targeted heal (never full agent restart)
- `src/api/boot_status.py` — `/api/boot_status` + `/api/boot_log` (<5ms cached)

| Stage | Label | Critical |
|-------|-------|----------|
| A | Core agent startup | yes |
| B | Feed acquisition | yes |
| C | External API readiness | yes |
| D | Routing warm-up | yes |
| E | Governance checks | yes |
| F | Execution loop activation | yes |
| G | Trade-readiness confirmation | yes |

**Trade-ready contract:** feeds live, routing armed, execution loop active, IG+Yahoo cached OK, governance clear, no critical subsystem failed.

**Cockpit:** `SplashScreen.tsx` polls `/api/boot_status` every 1.5s — stage list, subsystem heals, blockers, ETA.

### Verification

```bash
curl -s http://127.0.0.1:8080/api/boot_status | python3 -m json.tool
curl -s http://127.0.0.1:8080/api/boot_log?limit=20 | python3 -m json.tool
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_boot_orchestrator.py -q
```
