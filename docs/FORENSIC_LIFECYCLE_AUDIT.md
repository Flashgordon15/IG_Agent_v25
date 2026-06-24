# IG Agent v29.1 — Forensic Lifecycle Blueprint

**Mode:** Read-only forensic audit  
**Date:** 2026-06-23  
**Transport authority (Mac Mini):** `config/config_v25.json` → `"streaming_transport": "rest_poll"` (Lightstreamer code present but not active transport).

---

## Pre-Development Audit Snapshot

| Check | State |
|-------|-------|
| Market sessions | Night matrix 24/7 — no legacy blackout; rollover only 21:58–22:05 BST |
| Watchdog hold | `manual_stop.json` absent when audited |
| Active PIDs | Agent bound to `localhost:8080` |

---

## PHASE 1: LAUNCH & INITIALISATION

### 1.1 Boot Chain (Exact Spawn Order)

**Path A — Desktop cockpit**

```
scripts/launch_desktop_cockpit.sh
  └─ exec .venv/bin/python3 scripts/desktop_cockpit.py
       ├─ [optional] subprocess.Popen scripts/start_agent_background.sh  (if :8080 offline)
       ├─ threading.Thread(_gate_poll_loop)     — daemon, background
       ├─ threading.Thread(_shm_poll_loop)      — daemon, background
       └─ webview.start() / run_gui()           — GUI main thread (pywebview WebKit)
```

**Path B — Trading agent (`src/main.py`)**

```
src/main.py::main()
  ├─ run_preflight()           — lock, config, credentials, instance lock
  ├─ AgentRuntime(boot_context)
  ├─ uvicorn.Server.run()      — foreground OR daemon thread (harness mode)
  └─ Boot pipeline (Gate 1→5 via api/server create_app)
       ├─ bootstrap_credentials() → CredentialsHolder (process-global)
       ├─ build_market_orchestrator() → MarketOrchestrator
       ├─ start_market_stream() → IGStreamingClient thread (REST poll)
       ├─ orch.start() → per-epic TradingLoop threads + health monitor thread
       ├─ Gate 5: start_post_ready_services()
       │    └─ start_alpha_matrix_compiler_async() → _COMPILER_THREAD (daemon)
       └─ threading: fulfillment cache refresh, position sync, reconciler workers, etc.
```

**Independent background PIDs/processes vs in-process threads**

| Component | Model |
|-----------|-------|
| `src/main.py` | Single OS process; uvicorn on main or daemon thread |
| `scripts/start_agent_background.sh` | Spawns separate `caffeinate … python3 src/main.py` PID |
| Cockpit `desktop_cockpit.py` | Separate OS process; SHM reader threads only |
| All trading loops | In-process `threading.Thread` (one per epic) |
| REST poll stream | In-process `_thread` inside `IGStreamingClient` |
| Alpha matrix compiler | In-process `_COMPILER_THREAD` |

**Key file references**

- `scripts/launch_desktop_cockpit.sh:15` — exec cockpit
- `scripts/desktop_cockpit.py:433-436` — SHM/gate poll threads
- `src/main.py:665-668` — credentials bootstrap
- `src/runtime/market_orchestrator.py:541-606` — orchestrator start
- `src/trading/trading_loop.py:612-638` — per-epic loop threads

### 1.2 Shared Memory Allocation

**Alpha matrix segment** — name `ig_agent_v30_alpha_matrix`

| Constant | Value |
|----------|-------|
| `EPIC_SLOTS` | 8 |
| `RSI_BINS × ATR_BINS × MOM_BINS × DIR_SLOTS` | 32 × 16 × 16 × 2 |
| `TOTAL_CELLS` | 131,072 |
| `MATRIX_COLS` | 8 |
| Matrix payload | 131,072 × 8 × 4 = **4,194,304 bytes** |
| Header (`!QIIIIIIIddQQQQd`) | **92 bytes** |
| **`_SHM_TOTAL_BYTES`** | **4,194,396 bytes** |
| ndarray shape | `(131072, 8)`, `dtype=float32` |
| Locks | `_COMPILE_LOCK`, `_SEGMENT_LOCK` |

Source: `src/intelligence/matrix_prebaker.py:30-54`, `367-394`

**Cockpit telemetry segment** — name `ig_agent_v30_shm`

```
COCKPIT_SHM_ALLOC_BYTES = max(4096, CockpitShmHeader + 5×CockpitFillRow + StringPhaseDiag)
```

Source: `src/system/ipc/ring_buffer.py:693-698`

**PID-restart eviction (36963→47012 class)**

- Cockpit: `_evict_zombie_cockpit_shm()` — `ring_buffer.py:771-804`
- Alpha matrix: `flush_stale_alpha_matrix_shm()` — `matrix_prebaker.py:537-561`

**Identity bridges** (`ig_agent_v30_live_state`, `ig_agent_v30_shadow_state`, `ig_agent_v30_weight_xfer`) — JSON via `shared_memory_bridge.py`; purged on network teardown with `reset_shared_memory_bridge(unlink=True)`.

### 1.3 Environment / Credential Injection

**Load path:** `config/credentials/credentials.json` + `.env` overlay via `src/system/credentials_loader.py:102-148`

**Fields held in memory:**

```python
Credentials(
    ig_api_key, ig_username, ig_password,
    ig_account_type,  # DEMO | LIVE
    ig_account_id,
)
```

**Holder:** `CredentialsHolder` — loaded once in main preflight (`src/main.py:665`).

**Stream bootstrap:** `src/runtime/agent_bootstrap.py:713-718` — requires valid `rest_client.session`. Failure → `market stream skipped — no valid IG session`; loops block on `wait_stream_ready(timeout=120)`.

**Thread handoff:** `Credentials` passed to `create_streaming_client(creds, session, rest_client=…)`. REST singleton under `system.ig_rest_session._lock`. Poll thread holds same `rest_client` reference — password not re-copied into workers.

---

## PHASE 2: DATA STREAM INGESTION

### 2.1 Lightstreamer Connection (Present; Not Active on Mini)

| Constant | Value |
|----------|-------|
| `_MAX_LS_RECONNECT` | 10 |
| `_LS_RECONNECT_WAIT_SEC` | 5.0 |
| `_LS_RECONNECT_MAX_WAIT_SEC` | 60.0 |
| `_LS_CONNECTING_GRACE_SEC` | 90.0 |
| `_LS_BLANK_TICK_RECONNECT_SEC` | 30.0 |

Backoff: 5, 10, 20, 40, 60, 60… seconds. After 10 failures → REST poll fallback.

Source: `src/ig_api/lightstreamer_streaming.py:18-23`, `422-428`

**Active transport — REST poll**

| Constant | Value |
|----------|-------|
| `_PING_TIMEOUT_SEC` | 2.0 |
| `_max_backoff` | 30.0 |

Source: `src/ig_api/streaming_client.py:34`, `277-280`

**Factory:** `src/ig_api/streaming_factory.py:35-50`  
**Config lock:** `config/config_v25.json:330` → `"streaming_transport": "rest_poll"`

### 2.2 Ingestion Schema (Raw → Memory)

**Wire shape:**

```python
@dataclass
class PriceUpdate:
    epic: str
    bid: float
    offer: float
    timestamp: Any = None
```

Source: `src/ig_api/streaming_client.py:237-242`

**Callback → hub:** `src/runtime/agent_bootstrap.py:753-769`  
**Hub storage:** `QuoteSnapshot` in `_quotes[epic]` — `src/system/market_data_hub.py:287-311`

### 2.3 Multi-Epic Routing

- **Stream:** Single `IGStreamingClient`, one poll thread, all epics in `_epics` set — sequential per poll cycle.
- **Trading:** One `TradingLoop` thread per epic — parallel across epics, sequential within epic (`trading_loop.py:1343-1355`).

**Night matrix epic→slot:**

| Epic | Slot |
|------|------|
| `CS.D.CFPGOLD.CFP.IP` | 0 |
| `IX.D.DOW.IFM.IP` | 1 |
| `IX.D.NIKKEI.IFM.IP` | 2 |
| `CS.D.EURUSD.CFD.IP` | 3 |

Source: `src/intelligence/matrix_prebaker.py:56-63`

Lag risk: poll-interval × epic count per thread (not per-epic async queues at stream layer).

---

## PHASE 3: FEATURE MATRIX & ML INFERENCE

### 3.1 Rolling Buffer Update

Live ticks appended to `quotes_by_market[key]` list, trimmed to `max_live_quotes`. On-demand `pd.DataFrame` via `quote_df()`. Merged cap `_MAX_MERGED_TICKS` = 500.

Source: `src/signals/signal_engine.py:359-366`, `416-434`

### 3.2 Data Emptiness / NaN / ffill

**NaN sanitization:** `sanitize_matrix_nan_inf()` — zeros NaN/inf in-place (`matrix_prebaker.py:283-285`).

**Streaming ffill:** `matrix_row_with_streaming_ffill()` — mom_q walk; empty epic slot → `default_matrix_fallback_row()` (`matrix_prebaker.py:265-269`).

**Cold-start fallback row defaults:**

| Column | Default |
|--------|---------|
| signal_floor | 43.2 |
| fitness_floor | 43.2 |
| ml_floor | 0.40 |
| win_prob | 0.55 |
| approved | 1.0 |
| samples | 1.0 |

**Latency epics (bfill after ffill):** Nikkei, EUR/USD — `LATENCY_PACKET_FFILL_EPICS`.

### 3.3 Tensor / ML Interface

| Layer | Shape / type |
|-------|----------------|
| `FEATURE_STATE_DIM` | 128 |
| `compile_current_feature_state` | `np.zeros(128, dtype=float64)` |
| `probability_engine` | Expects `vector.size == 128`, else zeros |
| `ml_scorer.predict` | `dict[str,float]` → `pd.DataFrame([row])` → `predict_proba` → scalar ∈ [0,1] |
| Continuous opt worker | Raw `np.ndarray` vector |

Source: `src/signals/feature_state.py:10-52`, `src/trading/probability_engine.py:60-72`, `src/trading/ml_scorer.py:133-146`

**Mismatch handling:** 128-dim vector mapped via `_extract_ml_features`; missing keys → `return 0.5`.

---

## PHASE 4: EXECUTION GATEWAY & ORDER SCHEMAS

### 4.1 Order Trigger (Signal → IG REST)

```
TradingLoop (alpha or legacy gates)
  → gate_exec dict
  → execution/trading_loop.process_tick()
  → execution_engine.execute_trade()
  → LiveExecutor.execute()
  → IGRestClient.place_market_order()
  → POST /v1/positions/otc
```

**IG REST payload:**

```python
{
    "epic": epic,
    "expiry": "-",
    "direction": "BUY"|"SELL",
    "size": float,
    "orderType": "MARKET",
    "guaranteedStop": False,
    "forceOpen": True,
    "currencyCode": str,
    "stopDistance": float,
    "limitDistance": float,  # optional
}
```

Source: `src/ig_api/rest_client.py:1602-1636`

### 4.2 Schema Disconnect — `INTEGRITY_ABORT: missing gate_execution_params`

**Gate flag:** `integrity_gate_sourced_required()` ← `learning_demo_integrity_enabled()` (`economic_check.py:172-179`).

**Abort site:** `execution_engine.py:464-474` when `normalize_gate_execution_params()` returns `None`.

**Normalizer rejection:** `actual_size <= 0` or `stop_points <= 0` (`types.py:79-80`).

**Execution loop block:** `execution/trading_loop.py:454-466`.

**Fracture lines:**

| Path | Failure mode |
|------|--------------|
| Alpha dispatch | `gate_exec` built without `stop_points` until inner try (2458-2486); import failure → INTEGRITY_ABORT |
| Legacy gates | `_gate_execution_params_from_gates` returns `None` when `stop_points <= 0` or `size_int < 1` |
| Pre-fix logs | `gate_sourced=False`, `size=6.0`, `stop=0.0` on paths without `force_inject_gate_execution_params()` |

Gold and Wall St use identical schema; only `payload["epic"]` differs.

### 4.3 Simulation / Phantom Ledger (128 rows)

| Mechanism | Location |
|-----------|----------|
| `_PERF_ROWS` deque `maxlen=128` | `unified_fulfillment_cache.py:21` |
| Append with deal_id guard | `record_execution_performance_row()` — skips if `authentic_demo_broker_required()` and no deal_id |
| `dry_run` interceptor | `live_executor.py:150-151`, `_execute_dry_run` → `deal_reference="DRY-RUN"` |
| Shadow desk intercept | `rest_client.py:1515-1574` → `MOCK_SHADOW_ENTRY` |
| Phantom detection | `live_fire_ledger.py` — `_PHANTOM_SOURCES`, rows without `deal_id` |

---

## PHASE 5: RUNTIME STABILITY & FAULT MANAGEMENT

### 5.1 Mid-Window Restart (PID Change)

Generic handlers (no hard-coded PIDs):

1. `_evict_zombie_cockpit_shm` — unlink stale cockpit segment
2. `flush_stale_alpha_matrix_shm` — compare `alpha_matrix_publisher.pid` marker
3. Trading loop tick errors caught, loop continues (`trading_loop.py:1347-1353`)
4. Orchestrator `_loop_health_monitor` respawns dead loops
5. **No automatic stream cleanup on tick exception**
6. **Exit 137 (SIGKILL):** bypasses signal handlers; SHM may persist until next boot eviction

### 5.2 Error Suppression

Pattern: `except Exception: pass` or `log_guarded_exception` without re-raise.

~22 files under `src/execution/*`, ~7 under `src/ig_api/*`.

**Critical suppressions:**

- `signal_engine.refresh_hud_indicators` — silent HUD failure
- `trading_loop_alpha_gate_exec` — gate_exec may lack stop_points
- `trading_loop tick error (continuing)` — no alert, no stream reset

**Hard exit path:** `perform_network_failure_teardown` → flatten → SHM purge → `os._exit(0)` (`shutdown_cleanup.py:276-303`).

---

## PHASE 6: CLEAN CLOSE-DOWN

### 6.1 Signal Interception

| Handler | Location |
|---------|----------|
| `AgentRuntime` SIGTERM/SIGINT | `main.py:1083-1093` → `runtime.shutdown()` |
| Iron-clad harmonization | `clean_shutdown.py:118-136` → `perform_iron_clad_shutdown` + `perform_shutdown_cleanup` |
| API Stop button | `api/routes.py:980` → `os.kill(os.getpid(), SIGTERM)` |

### 6.2 Purge Sequence

**`perform_iron_clad_shutdown`:**

1. `stop_fulfillment_cache_refresh()`
2. `CapitalGuard._cancel_all_open_orders_and_positions(client)`
3. `stop_market_stream()`
4. `write_crash_state()`

**`perform_shutdown_cleanup`:**

1. `write_crash_state`
2. `stop_trading`
3. `stop_market_stream`, Yahoo poller, reconciler, position sync
4. Flight deck, health monitor, telegram schedulers
5. `LearningStore.checkpoint()`
6. `shutdown_shared_ig_session()`
7. `stop_watchdog`, `kill_other_agent_processes`
8. `force_release_instance_lock`, port cleanup

**Stream disconnect:** `agent_bootstrap.stop_market_stream()` → `target.disconnect()`

**SHM purge (aggressive path only):** `purge_shared_memory_segments()` — identity bridges unlinked; cockpit/alpha matrix **not** unlinked on graceful Stop.

### 6.3 Orphan Analysis

| Resource | Graceful shutdown | Hard kill (SIGKILL/137) |
|----------|-------------------|-------------------------|
| REST poll / LS WebSocket | `disconnect()` | Dies with process |
| `ig_agent_v30_shm` | Close in-process; unlink via zombie eviction only | Stale until `_evict_zombie_cockpit_shm` |
| `ig_agent_v30_alpha_matrix` | `force_unmap` on PID change only | Segment persists |
| Identity bridges | Unlink in `purge_shared_memory_segments` only | May survive Stop Agent |
| TradingLoop threads | `stop.set()` + join | Abrupt |
| Alpha compiler thread | Daemon — no explicit join | Dies with process |
| OrderConfirmWorker | Not explicitly cancelled in iron_clad path | May complete after stop |
| Instance lock | `force_release_instance_lock()` | Manual `rm -f` if stale |
| `get_shared_rest_client()` no-arg | May fail in clean_shutdown — flatten skipped | Same |

---

## Fracture Line Summary

```
Tick (REST poll thread)
  → hub.publish(epic, bid, offer)
  → TradingLoop._run_tick() [per-epic thread]
  → SignalEngine.evaluate() / alpha matrix lookup
  → gate_exec dict
  → execution/trading_loop.process_tick()
  → normalize_gate_execution_params()  ← FAIL if stop_points/size missing
  → integrity_gate_sourced_required()  ← TRUE in learning demo integrity mode
  → INTEGRITY_ABORT OR SUBMIT
  → LiveExecutor → place_market_order → POST /v1/positions/otc
```

---

## Operational Notes (Session Context)

- Fixes committed (`eb8bbd3`): gate injection, alpha matrix ffill, SHM PID eviction, phantom ledger sync, iron-clad sizing cap.
- `target_factory.sh` blocked by historical ledger P&L (£-5,350 / 36.2% / 229 closed), not code-only.
- Agent start tasks may exit **137** (wrapper SIGKILL) while child PID survives on `:8080`.
- Yahoo feed failures on Mini → `MockFeedEngine` fallback.
- `flight_deck_launch.sh` blocked by 3 pytest failures at time of audit.

---

*Generated from read-only repository interrogation. Authoritative specs: `IG_Agent_v29.1_COMPLETE_SPEC.md`, `docs/V29.1_ARCHITECTURE.md`.*
