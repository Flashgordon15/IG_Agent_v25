# v31 Runtime Mode System — Forensic Map

**Purpose:** Factual inventory of all runtime selectors, config overlays, startup paths, session identity, test modes, and mode combinations. No remediation proposals.

**Codebase:** Single binary (`src/main.py`), `APP_VERSION = "30.0.0"` (`system/identity/app_identity.py`). “Versions” below are **behavioral profiles**, not separate builds.

---

## 1. Runtime mode selectors (complete inventory)

### 1.1 Host / data plane — `ApexRuntimeMode`

| Value | Env aliases | Default when | Module |
|-------|-------------|--------------|--------|
| **PRODUCTION** | `IG_APEX_RUNTIME_MODE=PRODUCTION`, `PROD`, `LIVE` | Mac Mini / `NODE_ENV=production` | `system/apex_runtime_mode.py` |
| **SHADOW_LIVE** | `SHADOW_LIVE`, `SHADOW`; desktop shell | `IG_APEX_DESKTOP=1`, `IG_NODE_PROFILE=shadow` | same |
| **HARDENED_TESTBED** | `HARDENED_TESTBED`, `TESTBED`, `REPLAY` | Explicit env only | same + `testbed_firewall.py` |

Side effect: `apply_runtime_mode_to_environ()` arms testbed firewall or sets `IG_NODE_PROFILE=shadow`.

### 1.2 Broker / fill plane — `IG_AGENT_MODE`

| Value | Meaning | Order routing | Module |
|-------|---------|---------------|--------|
| **SHADOW** | Paper ledger | `ShadowExecutor` | `system/agent_execution_mode.py` |
| **DEMO** | IG demo REST | `LiveExecutor` → demo-api | same |
| **LIVE** | IG live REST | `LiveExecutor` (guarded by `demo_only_deployment`) | same |
| *(empty)* | Resolved at boot | → DEMO on Apex desktop / when mock disabled | `resolve_default_execution_mode_for_boot()` |

Related env flags (same module):

| Flag | Effect |
|------|--------|
| `IG_PRODUCTION_EXECUTION=1` | Forces real `IGRestClient`; sets `IG_AGENT_MODE=DEMO`, disables mock |
| `IG_MOCK_FEED=0/1` | Mock vs live feed |
| `IG_SESSION_VALIDATION=1` | Lower floors; clears circuit breaker on boot |
| `IG_SOAK_MODE=1` | File-triggered soak dispatch (`soak_live_fire.py`) |

### 1.3 Engine enum — `ExecutionMode`

| Value | Set when | Module |
|-------|----------|--------|
| **TEST** | Gate 4 build, no REST client | `execution/types.py` |
| **DEMO** | Gate 4 build with REST | same |
| **LIVE** | Config/credentials promote | same |

Distinct from `IG_AGENT_MODE`: this is **per orchestrator build** inside `build_market_orchestrator_instant()`.

### 1.4 Config `operating_mode` (merged JSON)

| Value | Source | Module |
|-------|--------|--------|
| **TEST** | Default when `dry_run: true` | `config_loader.py` |
| **DEMO** | `config_v29.json`, credentials sync | `_sync_operating_mode_from_credentials` |
| **LIVE** | Canary overlay, credentials | same |

Also: `config_loader.set_mode()` / `get_mode()` — in-process **TEST/DEMO/LIVE** string (`_MODE`), used by loader internals.

### 1.5 Node profile — `NodeKind`

| Kind | Port default | Data namespace | Module |
|------|--------------|----------------|--------|
| **production** | 8080 | `v31-production` / `learning_db.sqlite3` | `system/node_profile.py` |
| **shadow** | 9090 (9090) or 9199 (BootProfile) | `runtime_state_shadow.json`, shadow learning DB | same |
| **testbed** | 9199 | `testbed_state.json`, `testbed_ledger.db` | same |

Resolved from `ApexRuntimeMode` + `IG_NODE_PROFILE` + `NODE_ENV` + desktop shell.

### 1.6 Boot profile — immutable track (`BootProfile`)

| Track | Port | `IG_AGENT_MODE` | `IG_APEX_RUNTIME_MODE` | Mock |
|-------|------|-----------------|------------------------|------|
| **live** | 8080 | DEMO | PRODUCTION | off |
| **shadow** | 9199 | SHADOW | SHADOW | on |

Module: `system/identity/boot_profile.py`. Applied by `process_orchestrator`, `daemon_cycle_kernel`.

### 1.7 CLI / main.py entry selectors

| Selector | CLI / env | Mutually exclusive with |
|----------|-----------|-------------------------|
| Test harness | `--test-harness-ticks=N` → `IG_TEST_HARNESS=1` | `--daemon-cycle` |
| Daemon cycle | `--daemon-cycle=N` → `IG_DAEMON_CYCLE_SEC` | `--test-harness-ticks` |
| Isolated track | `--isolated-track=live\|shadow` + `IG_ORCHESTRATOR_CHILD=1` | — |
| Parallel dual | `IG_PARALLEL_DUAL=1` + daemon cycle | unified engine path |
| Unified engine | `IG_UNIFIED_ENGINE=1` (default **on**) + daemon cycle | dual parallel when unified off |
| Desktop | `IG_APEX_DESKTOP=1` | — |

### 1.8 Other material env flags (boot-shaping)

| Env | Stage applied | Effect |
|-----|---------------|--------|
| `IG_AGENT_CONFIG` | Config load | Overrides config file path |
| `IG_API_PORT` | Port / lock / node profile | Binds API; lock file suffix |
| `IG_NON_BLOCKING_BOOT` | main (default **1**) | Defer Gate 1 to lifespan |
| `IG_ORCHESTRATOR_CHILD` | main | Child of dual-track spawn |
| `IG_PARALLEL_TRACK` | `live` / `shadow` / `unified` | Track identity |
| `IG_TEST_HARNESS` | harness | Mock feed, port 9199, skip post-ready |
| `IG_AGENT_PYTEST` | pytest / gate5 subprocess | Skip locks, shutdown, many daemons |
| `IG_AGENT_FROM_LAUNCHER` | launcher scripts | `.env` override behaviour |
| `PROD_MODE=PRODUCTION` | supervisor → main | Used by `runtime_context` scripts; supervisor sets explicitly |
| `IG_SHARE_ENGINE=1` | supervisor | Share pacing; scripts set `IG_PRODUCTION_EXECUTION` |
| `IG_KERNEL_ARMED` | BootProfile | Kernel interceptor armed |
| `IG_BARE_METAL_EXEC` | unified engine | Bare-metal hot path |
| `IG_PREBAKED_ALPHA_MATRIX` | unified engine | Alpha matrix redirect |
| `TESTBED_ALLOW_ZOMBIE` | testbed | PID protection (`testbed_daemon.py`) |
| `IG_TESTBED_ROOT` | testbed | Isolated testbed directory |

---

## 2. Config overlays

### 2.1 Config files on disk

| File | Extends | Role today |
|------|---------|------------|
| `config/config_v31_live_canary.json` | `config_v31.json` | **Supervisor default** (`IG_AGENT_CONFIG`) |
| `config/config_v31.json` | `config_v30.json` | v31 production plane defaults |
| `config/config_v30.json` | `config_v29.json` | Apex monolith overlay |
| `config/config_v29.json` | `config_v25.json` | v29.1 DEMO deployment base |
| `config/config_v25.json` | *(instruments base)* | Instrument registry, epics |
| `config/config_v26.json` | `config_v25.json` | v26 profitability overlay (optional) |
| `config/config_v26_50k.json` | — | Vision / capital envelope variant |

**Loader chain** (`ConfigLoader._load_config_file`): recursive `$extends` deep-merge.  
**Primary path:** `IG_AGENT_CONFIG` → else `config_v30.json` → else `config_v29.json`.

**Canary merge chain (supervisor path):**  
`config_v31_live_canary.json` → `config_v31.json` → `config_v30.json` → `config_v29.json` → `config_v25.json`

### 2.2 Config overlay behavioural deltas

| Overlay | Key behavioural changes vs parent |
|---------|-----------------------------------|
| **v25 base** | Enabled instruments, epic list, core trading params |
| **v29** | `operating_mode: DEMO`, `demo_only_deployment: true`, `allow_live_trading: false`, protective_learning floors, `max_open_positions: 5` |
| **v30** | `allow_live_trading: true`, Yahoo reference pricing, session_validation block, micro_lot_verification, platform_v2 |
| **v31** | Execution sizing defaults (`max_daily_risk_loss: 400`) |
| **v31 live canary** | `operating_mode: LIVE`, `allow_live_trading: true`, `live_canary.*` bypasses, `max_open_positions: 1`, £5 loss envelope, `dual_core.forex_rotation_locked`, `broker_account_product: SPREADBET`, unlimited trade caps |

### 2.3 Config keys that gate execution paths

| Key block | Path A effect | Path B effect |
|-----------|---------------|---------------|
| `live_canary.enabled` | Baseline reset, scope guards | Risk parity guard |
| `live_canary.bypass_alpha_matrix` | Skips alpha-matrix tick redirect | None |
| `live_canary.bypass_traffic_governor` | REST governor | REST governor on micro POST |
| `dual_core.forex_rotation_locked` | Path A epic filter (hot path only) | Stack lock, no rotation |
| `dual_core.broker_account_product` | Via `resolve_order_epic` in LiveExecutor | Explicit in `_dispatch_micro_order` |
| `max_open_positions: 1` | Gate 9 risk_validation | `position_already_open` in micro dispatch |
| `execution.order_cadence_sec: 0` | — | Unlimited micro cadence |
| `entry_protection.max_trades_per_epic_per_session: 0` | Unlimited (with session inject) | Same inject at boot |

---

## 3. Startup path map

### 3.1 Production chain (`scripts/start.sh`)

```
┌─────────────────────────────────────────────────────────────────┐
│ start.sh                                                         │
│  PATH/PYTHONPATH → mkdir logs                                    │
│  pytest (6 modules, -p no:anyio)  ← NO agent, NO env selectors   │
└────────────────────────────┬────────────────────────────────────┘
                             │ pass
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ daemon_supervisor.sh (nohup)                                     │
│  Sets: PROD_MODE=PRODUCTION                                      │
│        IG_AGENT_CONFIG=config/config_v31_live_canary.json        │
│        IG_SHARE_ENGINE=1, IG_API_PORT=8080, PYTHONPATH=src       │
│  Spawns: python3 src/main.py                                     │
│  Writes: supervisor.pid, agent.pid                               │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ main.py                                                          │
│  os_surface_cleanse → socket singleton                           │
│  IG_NON_BLOCKING_BOOT=1 (default)                                │
│  load_dotenv → prepare_boot_env()                                │
│  ensure_production_execution_armed_on_boot()                     │
│  apply_runtime_mode_to_environ()  → PRODUCTION (unless overridden)│
│  apply_node_profile_to_environ()  → production, :8080            │
│  ConfigLoader(canary path) → merged config                         │
│  Gates G1–G5 → start_post_ready_services()                       │
│    → Path B armed, canary reset, Path A loops unpaused           │
└─────────────────────────────────────────────────────────────────┘
```

**Selectors applied per stage:**

| Stage | Selectors set / read |
|-------|----------------------|
| start.sh | None (pytest only) |
| daemon_supervisor | `PROD_MODE`, `IG_AGENT_CONFIG`, `IG_SHARE_ENGINE`, `IG_API_PORT`, `PYTHONPATH` |
| main.py early | `IG_NON_BLOCKING_BOOT`, harness/daemon CLI parse |
| main.py env | `.env`, `prepare_boot_env`, `IG_APEX_RUNTIME_MODE`, node profile |
| Gate 1 | `acquire_instance_lock()` for resolved port |
| Gate 2–3 | Credentials → `IGRestClient`; stream mode |
| Gate 4 | `ExecutionMode.DEMO` if REST present |
| Gate 5 | Unpause orchestrator; post-ready |
| post-ready | Path B + canary guards **unless** `IG_TEST_HARNESS=1` |

### 3.2 Alternate startup paths (same `main.py`)

| # | Entry | Key selectors | Config | Port |
|---|-------|---------------|--------|------|
| A | **Supervised canary** | `start.sh` chain above | canary JSON | 8080 |
| B | **Direct main** | env default | v30→v29 unless `IG_AGENT_CONFIG` | 8080 |
| C | **Test harness** | `--test-harness-ticks`, `IG_TEST_HARNESS=1`, mock feed | merged + harness env | 9199 |
| D | **Daemon cycle (legacy)** | `--daemon-cycle`, shadow boot profile | default | 8080 / detached log |
| E | **Dual parallel tracks** | `IG_PARALLEL_DUAL=1`, `IG_UNIFIED_ENGINE=0` | per-child | 8080 + 9199 |
| F | **Isolated child live** | `--isolated-track=live`, `BootProfile.for_live` | inherited | 8080 |
| G | **Isolated child shadow** | `--isolated-track=shadow`, `BootProfile.for_shadow` | inherited | 9199 |
| H | **Unified engine** | `IG_UNIFIED_ENGINE=1`, `--daemon-cycle`, bare metal | default | configurable |
| I | **Apex desktop** | `IG_APEX_DESKTOP=1` | default | desktop port |
| J | **HARDENED_TESTBED** | `IG_APEX_RUNTIME_MODE=HARDENED_TESTBED` | testbed paths injected | 9199 |
| K | **Pytest / CI** | `IG_AGENT_PYTEST=1` | test fixtures | varies |
| L | **Legacy launchd** | `install_launchd.sh`, `watchdog.sh` | varies | 8080 |

### 3.3 Distinct runtime shapes producible at boot

Counting **named, reachable behavioural shapes** (different port + host plane + broker plane + config + path arming):

| Shape ID | Profile | Path A | Path B |
|----------|---------|--------|--------|
| S1 | Supervised v31 canary PRODUCTION/DEMO | ✓ (7 loops) | ✓ |
| S2 | Direct main v31 default config | ✓ | ✓ |
| S3 | Direct main v29-only (no v30 file) | ✓ | ✓ |
| S4 | Test harness mock | ✓ (limited) | ✗ post-ready skipped |
| S5 | HARDENED_TESTBED | ✓ loops, REST blocked | ✗ REST blocked |
| S6 | SHADOW `IG_AGENT_MODE` + shadow node | ✓ ShadowExecutor | coordinator may start; no real broker |
| S7 | Dual track live child (:8080) | ✓ | ✓ |
| S8 | Dual track shadow child (:9199) | ✓ mock | ✗ typically no live post-ready parity |
| S9 | Unified engine bare-metal | ✓ alpha/bare paths | partial / different threading |
| S10 | Apex desktop + microkernel | ✓ | depends on post-ready |
| S11 | Daemon cycle detached | ✓ | ✓ if full G5 |
| S12 | IG_AGENT_PYTEST unit boot | partial / mocked | usually ✗ |

**Total distinct runtime shapes: 12 canonical profiles (S1–S12).**

Within S1 alone, **Path A sub-routes** add 3 tick variants (standard 12-gate, alpha matrix, soak/bare-metal) — but same process session.

---

## 4. Session identity

### 4.1 What defines a “session” today

| Layer | Identity mechanism | Scope |
|-------|-------------------|--------|
| **OS process** | Single `main.py` PID | One JVM-like agent lifetime |
| **API bind** | `IG_API_PORT` + instance lock `.ig_agent_v30_port_{port}.lock` | One listener per port |
| **IG broker session** | `get_shared_rest_client()` singleton | One login per process |
| **Supervisor** | `supervisor.pid`, `agent.pid` under `v31-production/` | Parent bash process |
| **Trading session (caps)** | In-memory + `LearningStore` runtime keys | Process lifetime |
| **Manual stop** | `manual_stop.json` | Blocks launchd restart ~10 min |

There is **no** separate session ID for DEMO vs LIVE vs SHADOW at the IG API layer beyond **which credentials/account** loaded at Gate 2.

### 4.2 Do DEMO / LIVE / SHADOW / TESTBED create separate sessions?

| Mode | Separate OS process? | Separate port/lock? | Separate IG login? | Separate data dir? |
|------|---------------------|---------------------|--------------------|--------------------|
| DEMO (agent) | Only if separate process | Yes if different port | Same process = shared | production node |
| LIVE (agent) | Same | Same | Same credentials path | production node |
| SHADOW (agent) | Typically **yes** (9199 child) | **Yes** | Mock / no IG | shadow paths |
| TESTBED | **Yes** (recommended) | **Yes** (9199) | **Blocked** | `testbed/` root |
| TEST harness | **Yes** (ephemeral) | **9199** | Mock | shadow-like |

### 4.3 Concurrent sessions on different ports

**Supported by design:**

- `BootProfile.for_live()` → :8080 + lock A  
- `BootProfile.for_shadow()` → :9199 + lock B  
- `launch_dual_tracks_detached()` → two `main.py` children  

**Conflict if both trade:** shared IG account, rate limits, position ambiguity.

**Blocked on same port:** `acquire_instance_lock()` fails if live sibling holds lock.

---

## 5. Testbed / simulator modes

| Mode | Env / entry | Shares code with DEMO/LIVE? | post_ready / Path B |
|------|-------------|----------------------------|---------------------|
| **IG_TEST_HARNESS** | `--test-harness-ticks` | Same gates, mock feed/REST | **Skipped entirely** |
| **IG_AGENT_PYTEST** | pytest | Same modules; mocked I/O | Skipped / partial |
| **HARDENED_TESTBED** | `IG_APEX_RUNTIME_MODE` | Same codebase; firewall panics prod I/O | REST blocked |
| **Historical replay** | `IG_HISTORICAL_REPLAY*`, replay scheduler | Feed path only | Depends on boot |
| **Testbed daemon PID** | `TESTBED_ALLOW_ZOMBIE` | Protects sim PID from kill scripts | N/A |

### 5.1 Test artefact persistence

| Artefact | Harness | Pytest | Testbed | Production |
|----------|---------|--------|---------|------------|
| `learning_db.sqlite3` | No / tmp | Often isolated | **testbed_ledger.db** | Yes |
| `triage_v31.db` | Unlikely | Can persist if written | Forbidden (panic) | Yes |
| Instance lock | Skipped | Skipped / tmp | Port-scoped | Yes |
| PID files | No | No | `/tmp/testbed_daemon.pid` | supervisor/agent.pid |
| Logs | Minimal | CI logs | `testbed.log` | `v31-production/logs/` |
| `broker_reject_guard` | In-memory | Reset in tests | In-memory | In-memory |

**start.sh pytest gate:** does **not** start an agent; **no session** created.

---

## 6. Mode interaction matrix

### 6.1 How layers combine (supervisor canary example)

```
PROD_MODE=PRODUCTION
  → (scripts only) may set IG_APEX_RUNTIME_MODE=PRODUCTION

IG_APEX_RUNTIME_MODE=PRODUCTION
  → NodeProfile kind=production, port 8080

IG_AGENT_MODE=DEMO (default after boot arming)
  → LiveExecutor → IG demo REST

config operating_mode=LIVE + allow_live_trading=true
  → LiveExecutor allows dispatch (demo_guard checks)

config live_canary.enabled=true
  → Baseline reset, Path A scope, Path B risk parity

ExecutionMode.DEMO (Gate 4)
  → Orchestrator built with broker client

post_ready (not harness)
  → Path B coordinator + sweep armed
  → Path A 7 loops unpaused
```

### 6.2 Combination validity

| Combination | Valid? | Notes |
|-------------|--------|-------|
| PRODUCTION + DEMO + canary | **Yes** | **Current v31 `start.sh` shape (S1)** |
| PRODUCTION + SHADOW (`IG_AGENT_MODE`) | Unusual | ShadowExecutor on production port — misconfiguration |
| HARDENED_TESTBED + LIVE broker | **Invalid** | Firewall panic on prod DB / REST |
| SHADOW_LIVE + DEMO broker | **Yes** | Shadow node, demo fills possible |
| Harness + canary config | **Yes** | post-ready skipped; Path B **not** armed |
| Dual :8080 + :9199 live+shadow | **Yes** | Two processes; **dangerous** if both hit IG |
| TESTBED + production config path | **Blocked** | Ledger path redirected / panic |
| `IG_AGENT_PYTEST` + live trading | **Partial** | Mocks prevent real orders in most tests |
| Unified engine + canary | Possible | Bare-metal / alpha paths dominate |
| PROD_MODE + `IG_SHARE_ENGINE` | Supervisor sets both | Operator scripts may force `IG_PRODUCTION_EXECUTION=1` |

### 6.3 Path A / Path B activation by combination

| Profile | Path A (12-gate loops) | Path B (micro-dispatch) |
|---------|------------------------|-------------------------|
| S1 Supervised canary | ✓ all enabled epics; non-hot WAIT | ✓ |
| S4 Test harness | ✓ fast tick | ✗ |
| S5 TESTBED | ✓ loops build | ✗ REST blocked |
| S6 SHADOW agent mode | ✓ ShadowExecutor | ✗ or mock only |
| S7 Live isolated child | ✓ | ✓ |
| S8 Shadow child | ✓ mock | ✗ |
| S9 Unified engine | ✓ bare-metal / alpha | Different threading model |
| K Pytest | Partial | ✗ |

**Path B arming condition (code):** `start_post_ready_services()` and not `_harness_mode()`.

**Micro-dispatch independence:** Once armed, runs on **separate threads** regardless of Path A gate state; shares `IGRestClient` and position sync.

---

## 7. Version count — direct answer

| Count type | Number | Definition |
|------------|--------|------------|
| **Shipped binaries** | **1** | `src/main.py` |
| **APP_VERSION label** | **1** | `30.0.0` (v31 is config/plane label) |
| **Host planes** | **3** | PRODUCTION, SHADOW_LIVE, HARDENED_TESTBED |
| **Agent modes (`IG_AGENT_MODE`)** | **3** | SHADOW, DEMO, LIVE |
| **Engine enums** | **3** | TEST, DEMO, LIVE |
| **Config operating_mode values** | **3** | TEST, DEMO, LIVE |
| **Node profiles** | **3** | production, shadow, testbed |
| **Boot entry profiles** | **12** | S1–S12 in §3.3 |
| **Config overlay files** | **7** | 2 active merge chains (default vs canary) |
| **Distinct behavioural agent “versions” today** | **12 reachable runtime shapes** | Boot entry × host × broker × config × path arming (§3.3) |

If counting **every theoretical env cross-product** (3×3×3×3×7 configs): **hundreds** — but most combinations are unreachable or fail-closed (testbed firewall, instance lock, mutual exclusive CLI).

**Operationally relevant “versions” on the Mac Mini host today: 2–3:**

1. **v31 supervised canary** (`start.sh` → supervisor → canary config) — primary  
2. **Legacy launchd/watchdog** path (still in repo) — alternate supervision  
3. **Developer/test shapes** (harness, pytest, testbed) — non-production  

---

## 8. Module index (selectors & activation)

| Concern | Module(s) |
|---------|-----------|
| Host plane | `system/apex_runtime_mode.py` |
| Broker plane | `system/agent_execution_mode.py` |
| Engine enum | `execution/types.py` |
| Config merge | `system/config_loader.py`, `config/config_*.json` |
| Node / paths | `system/node_profile.py`, `system/paths.py` |
| Boot profiles | `system/identity/boot_profile.py` |
| Dual spawn | `system/identity/process_orchestrator.py` |
| Unified engine | `system/unified_engine.py` |
| Test harness | `system/test_harness/runner.py` |
| Testbed firewall | `system/testbed_firewall.py` |
| Instance lock | `system/identity/instance_lock.py` |
| REST session | `system/ig_rest_session.py` |
| Supervisor | `scripts/daemon_supervisor.sh`, `scripts/start.sh` |
| Entry | `src/main.py` |
| Path A | `trading/trading_loop.py`, `execution/trading_loop.py` |
| Path B | `runtime/trade_manager.py`, `runtime/dual_core_execution.py` |
| Canary overlay | `runtime/live_canary_session.py`, `runtime/live_canary_guards.py` |
| Post-ready arming | `system/boot/post_ready_services.py` |

---

*End of runtime mode map.*
