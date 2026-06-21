# IG Agent v30 — Complete Specification (Authoritative)

**Version:** 30.0.0 · **Status:** SHIPPED · **Supersedes:** v24, v25, v29.0, v29.1 (all legacy spec text deprecated)

| Field | Value |
|-------|-------|
| Runtime identity | `APP_VERSION = 30.0.0` (`src/system/identity/app_identity.py`) |
| Config overlay | `config/config_v30.json` → extends v29 → v25 |
| Data plane | `~/Library/Application Support/IG Agent Apex/v30-production/` (macOS) |
| Live Vanguard | `:8080` · `IG_PARALLEL_TRACK=live` · `IG_APEX_RUNTIME_MODE=PRODUCTION` |
| Shadow Simulator | `:9199` · mock replay + weight training |
| Isolated Flight Deck | `:8787` · read-only SHM consumer (`ig_agent_v30_live_state`) |
| Orchestrator | Native `ParallelTrackSupervisor` (Python PID loop — no shell parsing) |

---

## 1. Deprecation notice

The following documents are **historical only** and must not govern runtime behaviour:

- `IG_Agent_v25_COMPLETE_SPEC_v8.md`
- `IG_Agent_v29.1_COMPLETE_SPEC.md`
- `docs/V29.1_ARCHITECTURE.md` (reference module map only)
- Legacy lock files (`.ig_agent_v29.lock`, `.ig_agent_v25.lock`, …)

**This document is the single source of truth for v30.**

---

## 2. Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Orchestrator grandchild (daemon-cycle parent)                  │
│  ParallelTrackSupervisor — os.kill(pid,0) every 5s              │
│    ├─ Live Vanguard    PID → :8080  (never auto-respawned)      │
│    ├─ Shadow Simulator PID → :9199  (native respawn on death)   │
│    └─ Isolated Cockpit PID → :8787  (read-only SHM telemetry)   │
└─────────────────────────────────────────────────────────────────┘
         │ shared memory                    │ weight xfer
         ▼                                  ▼
  ig_agent_v30_live_state          ig_agent_v30_weight_xfer
  ig_agent_v30_shadow_state
```

### Boot chain (Live Vanguard)

1. **Gate 1** — config + DEMO validation  
2. **Gate 2** — IG REST auth (**mock excision**: live PRODUCTION → `sys.exit(101)` on mock)  
3. **Gate 3** — streaming / reference pricing  
4. **Gate 4** — trading loops + telemetry (embedded Flight Deck suppressed when external cockpit armed)  
5. **Gate 5+** — execution, learning, protective floors  

### Night matrix (24/7 lockdown)

Epics: Gold, Wall St, Nikkei, EUR/USD — legacy weekday blackout **deleted**; rollover lock **21:58–22:05 BST** only.

---

## 3. Native multi-track supervision

Module: `src/system/identity/process_orchestrator.py`

| Class | Role |
|-------|------|
| `ParallelTrackSupervisor` | Non-blocking PID + port evaluation loop |
| `spawn_isolated_track()` | Detached live/shadow interpreters |
| `launch_dual_tracks_detached()` | Port reclaim + dual spawn + isolated cockpit |
| `run_parallel_supervisor_forever()` | Orchestrator entry — blocks until SIGTERM |

**Shadow death:** log + Telegram alert + respawn shadow only.  
**Live death:** CRITICAL log only — operator/launchd must restart (fail-closed capital protection).

Lock pointer: `~/.ig_agent_global/active_lock_pointer` → live `.ig_agent_v30_port_8080.lock`.

---

## 4. Isolated Flight Deck (:8787)

Module: `src/api/isolated_cockpit_server.py`

- Read-only consumer of `ig_agent_v30_live_state`
- Endpoints: `/`, `/api/health`, `/api/telemetry/live-state`, `/api/hub-quote-source`, `/ws/telemetry`
- **Never** calls `clear_port_8080`, genesis reset, or trading shutdown
- Spawned by orchestrator; recycled by `ParallelTrackSupervisor` without touching `:8080`

Embedded cockpit in Live Vanguard is suppressed when `IG_COCKPIT_ISOLATED_EXTERNAL=1`.

---

## 5. Quote provenance telemetry

Module: `src/system/market_data_hub.py` + `src/system/identity/state_cache.py`

Shared memory key: `hub_quote_source` — per night-matrix epic:

```json
{
  "CS.D.CFPGOLD.CFP.IP": {
    "source": "ig_rest|ig_execution|yahoo|synthetic",
    "staleness_seconds": 12
  }
}
```

Updated on every hub `publish()` for night-matrix epics. Seeded at Gate 2 completion.

---

## 6. Live path mock excision

Module: `src/system/guard/live_path_guard.py`

When `IG_PARALLEL_TRACK=live` **and** `IG_APEX_RUNTIME_MODE=PRODUCTION`:

- `MockFeedEngine` → blocked  
- `MockIGRest` → blocked  
- Gate 2 network failsafe bypass → blocked  
- Violation → `FailClosedSecurityError` + **`sys.exit(101)`**

Execution plane remains IG REST (`execution_transport: ig`, `ig_snapshot_at_execution: true`).

---

## 7. Chaos & certification

| Suite | Path | Requirement |
|-------|------|-------------|
| Chaos injectors | `tests/chaos_fuzzing_injector.py` | 8/8 PASS |
| Master journey | `tests/test_ultimate_journey.py` | Single sequential integration block |
| Stress S1–S3 | `tests/stress/` | Maintenance lockdown gates |

Launch command (production dual-track):

```bash
nohup env -i HOME="$HOME" PATH="$PATH" USER="$USER" \
  IG_APEX_RUNTIME_MODE="PRODUCTION" PYTHONPATH=src \
  .venv/bin/python3 src/main.py --daemon-cycle=900 \
  >>/tmp/ig_agent.orchestrator.log 2>&1 &
```

---

## 8. Operator quick reference

| Action | Command / URL |
|--------|----------------|
| Live dashboard | `http://127.0.0.1:8080/` |
| Flight Deck | `http://127.0.0.1:8787/` |
| Health | `curl -s http://127.0.0.1:8080/api/health` |
| PID registry | `/tmp/ig_agent_parallel.pids.json` |
| Live log | `/tmp/ig_agent.live.log` |
| Shadow log | `/tmp/ig_agent.shadow.log` |

**Do not run** `flight_deck_launch.sh` while live daemons are armed — it clears `:8080`.

---

*End of IG Agent v30 Complete Specification.*
