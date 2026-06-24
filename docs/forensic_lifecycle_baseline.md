# Forensic Architecture Profile & Baseline

**Document Reference:** `docs/forensic_lifecycle_baseline.md`  
**Version:** IG Agent v29.1 — Baseline State (2026-06-23)  
**Mode:** Read-only forensic profile (no runtime mutations implied)  
**Companion:** `docs/FORENSIC_LIFECYCLE_AUDIT.md` — phase-by-phase interrogation detail

---

## Part 1: Operational Blueprint & Source Registry

### 1. Architectural File Mapping & Boot Sequence

The system runs via **two independent, decoupled operating-system processes** interacting through:

1. **POSIX Shared Memory** (`multiprocessing.shared_memory.SharedMemory` segments)
2. **REST polling** on `127.0.0.1:8080` (agent API + IG price poll transport)

There is **no** shared Python interpreter between GUI and agent. The cockpit may optionally spawn the agent via `start_agent_background.sh` but does not embed trading logic.

---

#### Path A: The Frontend GUI (pywebview WebKit)

| Item | Value |
|------|-------|
| **Launcher** | `scripts/launch_desktop_cockpit.sh` |
| **Entry module** | `scripts/desktop_cockpit.py` |
| **Renderer** | WKWebView via `pywebview` (`gui="coco"`) |
| **HTML shell** | `scripts/cockpit_neon.html` |
| **Python** | `.venv/bin/python3` (required — system Python lacks pywebview) |
| **Window** | 1140×780, min 920×640, `#121214` background |

**Boot sequence (exact order)**

```
scripts/launch_desktop_cockpit.sh
  ├─ cd <repo-root>; export PYTHONPATH=src
  ├─ [preflight] curl :8080/api/health (2s timeout) — warn only
  └─ exec .venv/bin/python3 scripts/desktop_cockpit.py [args]
       ├─ main()
       │    ├─ [--no-preflight skipped] _preflight_ensure_agent()
       │    │    ├─ if health.agent_pid + boot_metrics.ready → return 0
       │    │    ├─ if manual_stop.json → exit 1
       │    │    └─ subprocess.Popen(start_agent_background.sh) — new session, detached
       │    └─ run_gui()
       │         ├─ webview.create_window(..., js_api=CockpitApi)
       │         ├─ _WINDOW.events.loaded → _on_loaded()
       │         └─ webview.start(gui="coco")  ← **blocks main thread**
       └─ _on_loaded() spawns:
            ├─ Thread(cockpit-gate-poll)  → _gate_poll_loop  (daemon, 2000ms)
            └─ Thread(cockpit-shm-poll)   → _shm_poll_loop   (daemon, 500ms)
```

**Thread model**

| Thread | Name | Interval | Role |
|--------|------|----------|------|
| Main | (pywebview) | — | Event loop; `webview.start()` |
| Background | `cockpit-gate-poll` | 2000 ms | Fetch `/api/unified/fulfillment` gate diagnostics |
| Background | `cockpit-shm-poll` | 500 ms | Read `ig_agent_v30_shm`, push `evaluate_js` to WebKit |

**Data ingress (GUI process)**

| Source | URI / segment | Fallback |
|--------|---------------|----------|
| Primary telemetry | SHM `ig_agent_v30_shm` | `read_cockpit_shm()` via `cockpit_shm_passive.py` |
| Gate grid | `GET /api/unified/fulfillment` | HTTP when SHM stale or `STATE_API_ONLY` |
| Health | `GET /api/health` | Agent liveness, boot percent |
| Heal | `POST /api/cockpit/heal` | Stall recovery actions |

**Linkage states** (`desktop_cockpit.py:47-53`)

| Constant | Meaning |
|----------|---------|
| `STATE_LIVE` | SHM publisher PID alive, fresh writes |
| `STATE_STALE_SHM` | Segment exists but publisher dead / PID mismatch |
| `STATE_AGENT_OFFLINE` | No agent on :8080 |
| `STATE_API_ONLY` | HTTP fallback rendering |
| `STATE_MANUAL_STOP` | `manual_stop.json` blocks auto-restart |
| `STATE_BOOTING` | Agent up but `boot_metrics.ready == false` |

**SHM read path (passive, no lock acquisition)**

```python
# scripts/desktop_cockpit.py:60-77
from system.ipc.cockpit_shm_passive import read_cockpit_shm
view = read_cockpit_shm()  # mmap ig_agent_v30_shm, parse CockpitShmHeader + fills
```

**Frame push (zero HTTP for live path)**

```python
# scripts/desktop_cockpit.py:422-426
js = f"window.updateFromShm({json.dumps(payload)});"
win.evaluate_js(js)
```

**Key source files — Path A**

| File | Lines (approx) | Responsibility |
|------|----------------|----------------|
| `scripts/launch_desktop_cockpit.sh` | 1–15 | Shell wrapper, PYTHONPATH, preflight curl |
| `scripts/desktop_cockpit.py` | 1–553 | GUI entry, poll loops, heal logic |
| `scripts/cockpit_neon.html` | — | WebKit UI, `updateFromShm()` handler |
| `src/system/ipc/cockpit_shm_passive.py` | 34–80 | `CockpitShmHeader`, `CockpitFillRow` ctypes layout |
| `src/system/ipc/ring_buffer.py` | 617–806 | SHM publisher (agent side), zombie eviction |
| `scripts/start_agent_background.sh` | 1–71 | Detached agent spawn from cockpit preflight |

---

#### Path B: The Trading Agent (FastAPI + MarketOrchestrator)

| Item | Value |
|------|-------|
| **Entry** | `src/main.py` |
| **API bind** | `127.0.0.1:8080` (live track) |
| **Config chain** | `config/config_v30.json` → `config_v29.json` → `config_v25.json` |
| **Version** | `src/system/app_identity.py` → `APP_VERSION = 29.1.0` (Apex v30 shell) |
| **Stream transport** | `config_v25.json` → `"streaming_transport": "rest_poll"` |

**Boot sequence (exact order)**

```
src/main.py::main()
  ├─ run_preflight()
  │    ├─ emergency_stop.lock check
  │    ├─ validate_config(merge_credentials)
  │    ├─ validate_demo_only_startup
  │    ├─ acquire_instance_lock() → .ig_agent_v30_port_8080.lock
  │    └─ bootstrap_credentials() → CredentialsHolder
  ├─ AgentRuntime(boot_context)
  ├─ _install_signal_handlers(runtime)  → SIGTERM/SIGINT
  ├─ uvicorn.Server(app).run()          ← foreground (normal) or daemon (harness)
  └─ api/server create_app(use_boot_pipeline=True)
       ├─ Gate 1 — preflight (credentials, config)
       ├─ Gate 2 — hydrate positions/orders, IG session
       ├─ Gate 3 — OHLC seed, signal engine warm
       ├─ Gate 4 — stream connect, hub first tick
       ├─ Gate 5 — READY, unpause loops, post_ready_services
       │    ├─ start_alpha_matrix_compiler_async()  → _COMPILER_THREAD
       │    ├─ fulfillment cache refresh thread
       │    └─ cockpit SHM publisher tick
       └─ build_market_orchestrator() + start()
            ├─ start_market_stream() → IGStreamingClient poll thread
            └─ per-epic TradingLoop.start() → _loop_thread + _silence_watchdog
```

**Detached launch (watchdog / cockpit)**

```
scripts/start_agent_background.sh
  ├─ resolve .venv/bin/python3
  ├─ caffeinate -i -s (if available)
  ├─ source config/credentials/launch.env (optional)
  ├─ purge .pyc / __pycache__
  └─ exec python3 src/main.py
```

**Thread model (agent process)**

| Thread | Name pattern | Role |
|--------|--------------|------|
| Main | uvicorn | HTTP API, boot pipeline |
| Stream | `ig-stream-poll` | REST price poll → `on_price` → hub |
| Per epic | `ig-agent-trading-loop-*` | `_run_tick()` signal + execution |
| Per epic | `ig-loop-watchdog-*` | Silence / stale-stream alert |
| Orchestrator | `ig-orchestrator-health` | Dead-loop respawn |
| Compiler | `alpha-matrix-compiler` | SHM matrix compile (300s interval) |
| Workers | various daemon | Order confirm, position sync, triage logger |

**Key source files — Path B**

| File | Responsibility |
|------|----------------|
| `src/main.py` | Process entry, preflight, uvicorn, signal handlers |
| `src/runtime/agent_bootstrap.py` | Orchestrator build, `start_market_stream`, `stop_market_stream` |
| `src/runtime/market_orchestrator.py` | Multi-epic loop container, health monitor |
| `src/trading/trading_loop.py` | Per-epic tick loop, gates, alpha matrix dispatch |
| `src/execution/trading_loop.py` | Execution-layer `process_tick`, INTEGRITY_ABORT gate |
| `src/execution/execution_engine.py` | `execute_trade`, param resolution |
| `src/execution/live_executor.py` | IG REST order submit, dry_run intercept |
| `src/ig_api/rest_client.py` | `place_market_order` → `POST /v1/positions/otc` |
| `src/ig_api/streaming_client.py` | REST poll transport, `PriceUpdate` schema |
| `src/ig_api/streaming_factory.py` | Transport resolution (auto/LS/rest_poll) |
| `src/system/market_data_hub.py` | Quote hub, `publish()` / `get_snapshot()` |
| `src/system/boot/post_ready_services.py` | Gate 5 service arm |
| `src/system/boot/gate5_runner.py` | READY transition |

---

### 2. POSIX Shared Memory Registry

| Segment name | Bytes (alloc) | Producer | Consumer | Lock |
|--------------|---------------|----------|----------|------|
| `ig_agent_v30_shm` | `max(4096, header+5×fill+diag)` | Agent `ring_buffer.py` | Cockpit `cockpit_shm_passive.py` | Header `write_seq`; hub `_lock` on publish |
| `ig_agent_v30_alpha_matrix` | **4,194,396** | `matrix_prebaker.py` | Trading loop lookup, compiler | `_COMPILE_LOCK`, `_SEGMENT_LOCK` |
| `ig_agent_v30_live_state` | JSON bridge | `shared_memory_bridge.py` | Dashboard / apex | `_bridge_lock` |
| `ig_agent_v30_shadow_state` | JSON bridge | same | Shadow track :9199 | same |
| `ig_agent_v30_weight_xfer` | JSON bridge | `weight_transfer_bridge.py` | ML weight handoff | `_bridge_lock` |

**Alpha matrix layout**

```
TOTAL_CELLS = 8 × 32 × 16 × 16 × 2 = 131,072
MATRIX_COLS = 8  (signal_floor, fitness_floor, ml_floor, win_prob,
                  approved, samples, rsi_anchor, atr_anchor)
ndarray shape: (131072, 8) float32
_HEADER_BYTES = 92  (!QIIIIIIIddQQQQd)
_SHM_TOTAL_BYTES = 92 + 131072×8×4 = 4,194,396
```

**Cockpit header fields** (`cockpit_shm_passive.py:47-73`)

`magic`, `version`, `write_seq`, `agent_pid`, `ticks_cached`, `signal_threshold`, `atr_multiplier`, `valve_status`, `last_trade_pnl`, `fill_count` + 5× `CockpitFillRow` ring.

**PID-restart hygiene**

| Trigger | Handler | File |
|---------|---------|------|
| Cockpit PID mismatch | `_evict_zombie_cockpit_shm` | `ring_buffer.py:771` |
| Alpha matrix PID file drift | `flush_stale_alpha_matrix_shm` | `matrix_prebaker.py:537` |
| Marker | `src/data/state/alpha_matrix_publisher.pid` | — |

---

### 3. Credential & Session Injection

| Stage | File | Mechanism |
|-------|------|-----------|
| Load | `src/system/credentials_loader.py` | `config/credentials/credentials.json` + `.env` |
| Hold | `src/system/credentials_holder.py` | Process-global `CredentialsHolder` |
| Bootstrap | `src/main.py:665` | `bootstrap_credentials()` in preflight |
| REST session | `src/system/ig_rest_session.py` | Singleton `IGRestClient` under `_lock` |
| Stream handoff | `src/runtime/agent_bootstrap.py:713` | `creds` + `rest_client.session` → `create_streaming_client` |

**Failure mode:** `creds is None` or `session.is_valid == False` → stream skipped; loops block on `wait_stream_ready(timeout=120)`.

---

## Part 2: Data Ingestion Pipeline

### Tick path (REST poll — active transport)

```
IG REST /markets/{epic}
  → IGStreamingClient._poll_loop (single thread, all epics in _epics set)
  → PriceUpdate(epic, bid, offer, timestamp)
  → agent_bootstrap.on_price()
  → MarketDataHub.publish() / publish_replay_tick()
  → QuoteSnapshot in hub._quotes[epic]
  → TradingLoop._run_tick() reads hub.get_snapshot(epic)
  → SignalEngine.add_quote()
```

**Lightstreamer** (`src/ig_api/lightstreamer_streaming.py`) — present, not active on Mac Mini:

- Reconnect: max 10 attempts, backoff 5→60s
- Grace: 90s connecting, 30s blank-tick recovery
- Fallback: REST poll after exhaustion

**Multi-epic concurrency:** parallel across epics (one thread each), sequential within epic at `_tick_interval`.

---

## Part 3: Feature Matrix & ML

| Layer | Dim / type | File |
|-------|------------|------|
| Live quote buffer | list[Quote], capped `max_live_quotes` | `signal_engine.py:359` |
| Feature state | 128 × float64 | `feature_state.py:10` |
| Alpha matrix cell | 8 × float32 | `matrix_prebaker.py` |
| ML scorer input | named `dict[str,float]` → DataFrame row | `ml_scorer.py:133` |
| Probability blend | 0.55×ML + 0.45×continuous opt | `probability_engine.py:84` |

**Packet loss / empty cells:** `sanitize_matrix_nan_inf`, `matrix_row_with_streaming_ffill`, `default_matrix_fallback_row` for cold Nikkei/EURUSD slots.

---

## Part 4: Execution Gateway

### Order schema (IG REST)

```json
{
  "epic": "CS.D.CFPGOLD.CFP.IP",
  "expiry": "-",
  "direction": "BUY",
  "size": 1.0,
  "orderType": "MARKET",
  "guaranteedStop": false,
  "forceOpen": true,
  "currencyCode": "GBP",
  "stopDistance": 10.0,
  "limitDistance": 20.0
}
```

### Integrity gate (`INTEGRITY_ABORT`)

Enabled when `learning_demo_integrity_enabled()` → requires `normalize_gate_execution_params()` with `gate_sourced: true`, `actual_size > 0`, `stop_points > 0`.

**Fracture:** alpha path builds `gate_exec` before `stop_points` assignment; import failure in iron-clad block leaves params invalid.

### Simulation interceptors

| Mode | Path | Effect |
|------|------|--------|
| `dry_run` | `live_executor.py:150` | `DRY-RUN` deal ref, no REST |
| Shadow desk | `rest_client.py:1515` | `MOCK_SHADOW_ENTRY` |
| Phantom rows | `unified_fulfillment_cache.py` | `_PERF_ROWS` deque maxlen=128, no deal_id flagged |

---

## Part 5: Stability & Fault Management

- Tick errors: caught, loop continues (`trading_loop.py:1347`)
- Wrapper exit 137: SIGKILL on shell; child agent may survive on :8080
- Network teardown: `perform_network_failure_teardown` → flatten → SHM purge → `os._exit(0)`
- Error suppression: widespread `except Exception: pass` in execution/ig_api (~29 files)

---

## Part 6: Shutdown Lifecycle

| Signal | Handler |
|--------|---------|
| SIGTERM/SIGINT | `main.py:1083` → `AgentRuntime.shutdown()` → `perform_shutdown_cleanup` |
| Iron-clad | `clean_shutdown.py:118` → flatten → `stop_market_stream` → crash_state |

**Gaps:** cockpit/alpha SHM not unlinked on graceful Stop; `OrderConfirmWorker` not explicitly cancelled; hard kill leaves orphan segments until next PID eviction.

---

## Appendix A: Night Matrix Epics (24/7 Lockdown)

| Epic | SHM slot |
|------|----------|
| `CS.D.CFPGOLD.CFP.IP` | 0 |
| `IX.D.DOW.IFM.IP` | 1 |
| `IX.D.NIKKEI.IFM.IP` | 2 |
| `CS.D.EURUSD.CFD.IP` | 3 |

Rollover lock only: **21:58–22:05 BST**. Legacy weekday blackout **deleted**.

---

## Appendix B: Baseline Operational State (2026-06-23)

| Metric | Value |
|--------|-------|
| Branch | 2 commits ahead of `origin/main` (`eb8bbd3`) |
| Transport | `rest_poll` |
| Yahoo feed | Fails on Mini → `MockFeedEngine` |
| Ledger (target_factory) | £-5,350 / 36.2% / 229 closed |
| Agent start pattern | Wrapper exit 137; child PID may persist |
| flight_deck_launch | Blocked by 3 pytest failures at audit time |

---

*Baseline frozen 2026-06-23. Update when boot chain, SHM layout, or transport config changes.*
