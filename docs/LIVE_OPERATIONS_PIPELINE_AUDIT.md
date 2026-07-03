# Live Operations Pipeline — Structural Audit (Read-Only)

**Project:** IG Agent v29.1  
**Scope:** Programming hooks, method names, loop variables, and API origins for autonomous self-healing layer design  
**Constraint:** Read-only audit — no code modified  
**Audit date:** 2026-07-01  
**Prepared for:** Copilot review and next-step planning

---

## Executive Topology

```
[Feed producers] → MarketDataHub.publish / enqueue_stream_frame
                → hub.on_quote listeners
                → ApexMicrokernel.on_tick_ingest → Worker A (_ingest_q) → B → C → D

[MasterOrchestrator] _dispatcher_loop (500ms) → dispatch_market_updates → _dispatch_single_update → resolve_execution_route

[DualCoreExecution] execute_parallel_strategy_sweep (500ms) + hub tick lane (_on_hub_quote_micro_scalper_lane)

[ChaosGuardian] _daemon_loop (1s) → run_state_reconcile_tick → broker_reconciliation_daemon.run_reconciliation_once

[Cockpit :8787] create_cockpit_app REST proxies → in-process snapshot getters
[Main API :8080] routes.router → same snapshot getters (+ iron_cage_status)
```

---

## 1. Web-Socket & Stream Ingestion Boundaries

### 1.1 MarketDataHub — central quote cache

**File:** `src/system/market_data_hub.py`

| Symbol | Role |
|--------|------|
| `class MarketDataHub` | Thread-safe per-epic quote cache |
| `_stream_frame_queue` | `queue.SimpleQueue` — non-blocking websocket frame buffer |
| `_stream_consumer_stop` | `threading.Event` — consumer shutdown latch |
| `_stream_consumer_thread` | Background drain thread |
| `_stream_frames_ingested` / `_stream_frames_dropped` | Frame metrics counters |
| `_listeners` | Pub/sub for quote updates |
| `NIGHT_MATRIX_EPICS` | Canonical 24/7 epic universe tuple |

**Handshake / consumer lifecycle hooks:**

| Method | Thread name | Behavior |
|--------|-------------|----------|
| `enqueue_stream_frame(epic, bid, offer, *, source="websocket", quote_time=None)` | Producer (any) | `put_nowait` on `_stream_frame_queue`; calls `_ensure_stream_consumer()` |
| `_ensure_stream_consumer()` | Spawns | `threading.Thread(target=self._stream_consumer_loop, name="HubStreamFrameConsumer", daemon=True)` |
| `_stream_consumer_loop()` | `HubStreamFrameConsumer` | `get(timeout=0.05)` → `publish()` → microkernel ingest |
| `start_stream_frame_consumer()` | — | Idempotent consumer start |
| `stop_stream_frame_consumer()` | — | Sets stop event, `join(timeout=1.0)` |
| `stream_frame_metrics()` | — | Returns `queue_depth_approx`, `frames_ingested`, `frames_dropped`, `consumer_alive` |

**Note:** `enqueue_stream_frame` is defined but has no in-repo callers yet — designated hook for high-frequency websocket producers. Live paths today use `publish()` directly.

**Primary publish path (`publish`):**

1. `validate_quote_packet_fast` → `reject_packet_code` on failure
2. `should_publish_live_quote` gate
3. In-place `QuoteSnapshot.refresh` or new snapshot in `_quotes`
4. `rest.touch_stream_activity()` if REST attached
5. `signal_stream_ready(source=f"hub_publish:{epic}")` on first valid tick
6. `_emit_quote(snap)` → all `on_quote` listeners via `guard_call("hub_quote_listener", cb, snap)`
7. `_sync_hub_quote_source_metric` for night-matrix epics

**Listener registration:** `on_quote(callback) -> Callable[[], None]` (unsubscribe closure)  
**Singleton accessor:** `get_market_data_hub()`

### 1.2 Lightstreamer — IG websocket transport

**File:** `src/ig_api/lightstreamer_streaming.py`

| Method | Role |
|--------|------|
| `_connect_lightstreamer()` | SDK connect + subscription bind |
| `_teardown_lightstreamer()` | Clean disconnect |
| `_schedule_lightstreamer_reconnect()` | Thread `IGLightstreamerReconnect`; exponential backoff 5→60s |
| `_schedule_blank_tick_recovery(epic)` | Thread `IGLightstreamerBlankRecovery` |
| `_start_fallback()` | Falls back to REST poll after LS exhaustion |
| `_mark_connected_on_first_tick(bid, offer, epic)` | Handshake completion on first valid tick |

**Raw frame unpack → hub:** `get_market_data_hub().publish(resolved_epic, bid, offer, source="lightstreamer")`

**Transport selection:** `src/ig_api/streaming_factory.py` — config key `streaming_transport` (`lightstreamer` vs `rest_poll`).

### 1.3 DataFeedOrchestrator — multi-source feed supervisor

**File:** `src/system/feeds/data_feed_orchestrator.py`

| Symbol | Role |
|--------|------|
| `_PRIMARY_ORDER` | `("yahoo", "finnhub", "twelve_data")` |
| `_SIGNAL_MAX_AGE_SEC` | 45 (env `IG_SIGNAL_QUOTE_MAX_AGE_SEC`) |
| `_retry_thread` / `_retry_stop` | Daemon retry loop control |
| `start_data_feed_orchestrator(epics, *, cfg)` | Arms Yahoo poller, multi-feed hub, bootstrap, retry |
| `_bootstrap_sync(epics)` | Staggered epic poll at boot |
| `_retry_loop()` | Degraded health → chaos guardian reconnect macros |
| `get_data_feed_state()` | Composed health snapshot (`/api/data_feed_state`) |

**Reconnect channels:** `yahoo_feed`, `finnhub_ws`, `ig_stream` via `notify_channel_disconnected`, `should_delay_reconnect`, `compute_reconnect_delay`.

**Yahoo publish path:** `src/feeder/yahoo_quote_poller.py` → `get_market_data_hub().publish(...)`

### 1.4 Microkernel ingest boundary

**File:** `src/apex/microkernel.py`

| Caller | Method | Queue |
|--------|--------|-------|
| `HubStreamFrameConsumer` | `get_microkernel().on_tick_ingest(epic, dict)` | `_ingest_q` |
| `_on_hub_quote_micro_scalper_lane` | `get_microkernel().on_tick_ingest(epic, snap)` | `_ingest_q` |

**`on_tick_ingest` pipeline:** `_resolve_tick_flow_context` → `TickFrame` → `enqueue_ingest_frame` → `_ingest_q.put_nowait`

**Worker A:** `_worker_a_ingest()` — `frame = self._ingest_q.get()`, coalesce `_ingest_coalesce`, `_append_ring`, forward `_math_q`

**Workers:** A (ingest), B (math), C (risk), D (ledger) — started by `start_microkernel()`

### 1.5 Dual-core streaming tick lane

**File:** `src/runtime/dual_core_execution.py`

| Constant | Value |
|----------|-------|
| `STACKED_POLL_SEC` | 0.5 (500ms) |
| `ROTATION_SWEEP_SEC` | `STACKED_POLL_SEC` |
| `SOCKET_STALE_SEC` | 5s stale threshold |

| Method | Role |
|--------|------|
| `_on_hub_quote_micro_scalper_lane(snap)` | Hub listener → microkernel + instant scalp |
| `start_micro_scalper_tick_lane()` | Registers `hub.on_quote` |
| `evaluate_multi_source_rotation_sweep(cfg)` | 500ms universe ingest |
| `validate_socket_heartbeat()` | Flags `SOCKET_STALE`, triggers rehydration |

---

## 2. Runtime Error Trapping & Dispatch Gates

### 2.1 MasterOrchestrator

**File:** `src/runtime/master_orchestrator.py`

| Constant | Value |
|----------|-------|
| `_ROUTE_REFRESH_SEC` | 0.5 (500ms dispatcher) |
| `_DROPPED_EPIC_TTL_SEC` | 60.0 |
| `_dispatcher_thread` | `name="master-orchestrator-dispatch"` |

**`_dispatcher_loop`:** every 500ms → `asyncio.run(dispatch_market_updates([]))` → on exception `record_asset_stream_failure("_dispatcher_loop", ...)`

**Dispatch chain:**

- `dispatch_market_updates` — `asyncio.gather(..., return_exceptions=True)`
- `_dispatch_single_update(epic, bid, offer)` — packet validate, `resolve_execution_route` via `asyncio.to_thread`; exceptions → `_drop_epic_temporarily`
- `resolve_execution_route(epic)` — Markov regime, tuner gate, `validate_regime_entropy_arbitration`, scoreboard multipliers

**`_drop_epic_temporarily`:** writes `_dropped_epics`, `_last_dispatch_errors`, calls `record_asset_stream_failure(key, reason)`

### 2.2 DualCoreExecution — 500ms strategy sweep

**File:** `src/runtime/dual_core_execution.py`

**Loop:** `execute_parallel_strategy_sweep(*, cfg, stop_event, interval_sec=ROTATION_SWEEP_SEC)`

Per iteration: `validate_socket_heartbeat` → `evaluate_multi_source_rotation_sweep` → per-slot quotes → piercing zone dispatch → `evaluate_failover_tick_health`

**Logger:** `ig_agent.parallel_strategy_sweep`

**Gates:** `guard_path_b_handoff`, `hard_guard_path_b_handoff`, `set_last_gate_suppression_reason`

### 2.3 Chaos Guardian stream failure telemetry

**File:** `src/system/chaos_guardian.py`

`record_asset_stream_failure(epic, reason)` → `_asset_stream_failures` deque → exposed in `get_guardian_status_snapshot()["asset_stream_failures"]`

**Callers:** `_drop_epic_temporarily`, `_dispatcher_loop`

---

## 3. Ledger Reconciliation & Execution Floating-Points

### 3.1 Broker reconciliation daemon

**File:** `src/system/broker_reconciliation_daemon.py`

| Constant | Value |
|----------|-------|
| `_INTERVAL_SEC` | 1.0 (~1000ms) |
| `_thread` | `name="broker-reconcile"` |

**Core:** `run_reconciliation_once(*, rest)` — compares `_count_broker_positions` vs `_count_internal_positions`; `drift = abs(broker_n - internal_n)`; healthy if `drift <= 1`

**Kill-switch:** `trip_master_strategy_kill_switch(deal_id="", reason=f"reconcile_drift:{reason}")` when drift > 1

**API:** `get_reconciliation_snapshot()` → `/api/reconciliation_state`

### 3.2 Chaos Guardian reconcile orchestration

**File:** `src/system/chaos_guardian.py`

| Constant | Value |
|----------|-------|
| `_RECONCILE_INTERVAL_SEC` | 1.0 |

**`_daemon_loop`:** every 1s → `run_state_reconcile_tick(rest=_rest_client)` → `_refresh_snapshot()`

**`run_state_reconcile_tick` flow:**

1. `flags = _local_anomaly_flags()` — local-first, no REST if empty
2. If flags → `acquire_outbound_token("ig", category="ledger")`
3. `run_reconciliation_once(rest=client)`
4. If `drift_count > 1` → `_state_sync_discrepancies`, `_emergency_flatten_drift`

**`_local_anomaly_flags` sources:**

- `reconcile_cache_drift` from broker daemon snapshot
- `lifecycle_tree_mismatch` — `_local_position_tree()` vs `list_active_lifecycle_trades`
- `order_timeout` — `pending_order_reconcile.is_unresolved_overdue`

**`_local_position_tree`:** exploration `position_tree` first, fallback `trade_lifecycle.snapshot()["active"]`

**Registers:** `preallocate_reconciliation_registers`, `get_reconciliation_register_snapshot`

### 3.3 Emergency flatten cascade

**`_emergency_flatten_drift`:** token acquire → `rest.open_positions()` → `rest.close_position` per deal → `notify_emergency_flatten` → `trip_master_strategy_kill_switch(reason=f"guardian_flatten:{reason}")`

**Reconnect macros:** `notify_channel_connected`, `notify_channel_disconnected`, `should_delay_reconnect`, `compute_reconnect_delay`

### 3.4 Iron Cage readiness

**File:** `src/system/iron_cage_readiness.py`

`evaluate_iron_cage_readiness()` — 1s cache TTL; `broker_reconciliation_drift` blocker from `get_reconciliation_snapshot()`

### 3.5 Floating-point surfaces

| Location | FP handling |
|----------|-------------|
| `MarketDataHub.publish` | `float(bid/offer)`, spread = offer - bid |
| `microkernel.on_tick_ingest` | float coercion, `_maybe_chaos_widen_quote` |
| `resolve_execution_route` | confidence, size_factor_mult, kelly caps |
| Reconciliation | integer position **count** drift (not price-level) |
| ML path | 128-dim `np.float64` in `feature_state` |

---

## 4. Cockpit Server & API Origins

### 4.1 Two-server architecture

| Server | Port | Entry | UI |
|--------|------|-------|-----|
| Live Vanguard API | 8080 | `src/api/server.py` | React dashboard |
| Flight Deck Cockpit | 8787 | `src/cockpit/web_server.py` | `cockpit-web/` |

**Cockpit start:** `start_cockpit_web_server(port, hz=2.5)` — uvicorn `127.0.0.1:8787`

**Isolated variant:** `src/api/isolated_cockpit_server.py` — shared memory only, never binds :8080

### 4.2 Main API (`src/api/routes.py` on :8080)

| Endpoint | Backend getter |
|----------|----------------|
| `GET /api/orchestrator_state` | `get_orchestrator_state_snapshot()` |
| `GET /api/guardian_status` | `get_guardian_status_snapshot()` |
| `GET /api/iron_cage_status` | `evaluate_iron_cage_readiness()` |
| `GET /api/reporting_status` | `get_reporting_status_snapshot()` |
| `GET /api/reconciliation_state` | `get_reconciliation_snapshot()` |
| `GET /api/data_feed_state` | `get_data_feed_state()` |

### 4.3 Flight Deck (`src/cockpit/web_server.py` on :8787)

Proxies (with `sanitize_for_ws_json`): orchestrator_state, guardian_status, reporting_status, macro_steering

**Not proxied on :8787:** `/api/iron_cage_status` (main API only)

**WebSockets:** `/ws/telemetry` (2.5 Hz), `/ws/logs`, `/ws/triage`

**Emergency:** `POST /api/emergency` → `EMERGENCY_FLATTEN` command queue

---

## 5. Self-Healing Extension Points (Design-Ready)

| Layer | Observe / inject | Key symbols |
|-------|------------------|-------------|
| Stream ingest | Queue depth, drops | `stream_frame_metrics`, `enqueue_stream_frame`, `_stream_consumer_loop` |
| Feed health | Provider retry | `get_data_feed_state`, `_retry_loop`, `notify_channel_*` |
| Tick → microkernel | Ingest drops | `on_tick_ingest`, `_stats["math_dropped"]`, `_worker_a_ingest` |
| Route dispatch | Per-epic failures | `_drop_epic_temporarily`, `_last_dispatch_errors` |
| Strategy sweep | Stale sockets | `validate_socket_heartbeat`, `SOCKET_STALE` |
| Reconcile | 1s drift | `run_reconciliation_once`, `run_state_reconcile_tick` |
| Emergency | Flatten + halt | `_emergency_flatten_drift`, `trip_master_strategy_kill_switch` |
| Telemetry | REST + WS | :8080 routes, :8787 proxies, iron_cage readiness |

---

## 6. Timing Reference

| Loop | Interval | Thread / async name |
|------|----------|---------------------|
| Master route dispatcher | 0.5s | `master-orchestrator-dispatch` |
| Dual-core strategy sweep | 0.5s | `execute_parallel_strategy_sweep` |
| Chaos guardian reconcile | 1.0s | `chaos-guardian` |
| Broker reconcile daemon | 1.0s | `broker-reconcile` |
| Hub stream consumer | 50ms poll | `HubStreamFrameConsumer` |
| Cockpit telemetry WS | 2.5 Hz | uvicorn websocket |
| Iron cage cache | 1.0s TTL | `evaluate_iron_cage_readiness` |

---

*End of audit document*
