# v31 Live Canary — End-to-End Architecture, Runtime Performance & Gap Analysis

**Document purpose:** Explain how the IG Agent v31 live canary (IG DEMO) is *designed* to trade, how it is *actually behaving* on the host today, and where logic conflicts or wiring gaps prevent real fills. Use this as the primary reference for remediation.

| Field | Value |
|-------|-------|
| Profile | `config/config_v31_live_canary.json` (via `IG_AGENT_CONFIG` in `scripts/daemon_supervisor.sh`) |
| Operating mode | `LIVE` / `allow_live_trading: true` |
| Host API | `http://127.0.0.1:8080` |
| Snapshot date | 2026-06-28 (live telemetry pulled during doc authoring) |

---

## 1. Executive summary — why no real trades are occurring

**The agent is not idle.** Path B (dual-core micro-scalper) is armed, processing ticks (~20k+), and **has attempted dozens of broker orders**. Telemetry at authoring time showed **50 consecutive ledger rows, all `REJECTED`**, with IG reason:

> `INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY`

Confirm payloads show orders posted against **`CS.D.GBPUSD.CFD.IP` / `CS.D.EURUSD.CFD.IP`** — not spread-bet epics (`.TODAY.IP`). On a UK **spread-betting** IG account, CFD epics are rejected with exactly this error.

**Primary root cause (P0):** `dual_core.broker_account_product: "auto"` fails to resolve the account as `SPREADBET` at boot and/or dispatch, so `resolve_order_epic()` leaves CFD instrument codes on the wire.

**Secondary causes (P1–P3):**

| Priority | Issue | Effect |
|----------|-------|--------|
| P1 | Z-score rarely in piercing band (\|Z\| ≥ 2.0) | Most ticks produce `core_b_micro_active` but no dispatch (Z ≈ 0.4–0.6 observed) |
| P2 | Path A vs Path B share `max_open_positions: 1` with different gate stacks | Macro path blocked while micro path churns rejections; no coordinated “single slot” owner |
| P3 | Path A still runs 7 orchestrator epics with strict 12-gate + £5 loss envelope | Macro entries rarely reach LiveExecutor even when Path B fails |

**Bottom line:** Architecture is *partially operational* — signal → REST POST → confirm loop works — but **broker instrument mapping is wrong**, so every order dies at IG confirm. Fixing epic/product resolution is the highest-leverage change.

---

## 2. System topology (intended)

Two **parallel trading planes** share one IG account, one REST client, and one position slot (`max_open_positions: 1`).

```mermaid
flowchart TB
  subgraph ingress [Market data]
    YH[Yahoo heartbeat / rest_poll]
    HUB[MarketDataHub snapshots]
    OHLC[OHLC cache per epic]
  end

  subgraph boot [Boot G1–G5]
    G1[Preflight]
    G2[Broker handshake]
    G3[Stream coupled]
    G4[Orchestrator build — loops paused]
    G5[READY — unpause loops]
    PR[post_ready_services]
  end

  subgraph pathA [Path A — 12-gate macro]
    MO[MarketOrchestrator]
    TL[TradingLoop × 7 epics]
    G12[12 gates + pre-gates]
    SE[SignalEngine]
    RM[RiskManager]
    LE[LiveExecutor → confirm_deal]
  end

  subgraph pathB [Path B — dual-core micro]
    DC[DualCoreCoordinator thread]
    SW[Stacked 500ms sweep]
    Z[Z-score / micro channel]
    DM[_dispatch_micro_order]
    VS[Virtual stop 2pt watchdog]
  end

  subgraph broker [IG REST]
    POST[POST /positions/otc]
    CONF[GET /confirms/{ref}]
  end

  subgraph persist [Observability]
    TRIAGE[(triage_v31.db production_orders)]
    TEL[/api/v31/telemetry]
    LOG[engine + strategy_eval logs]
  end

  YH --> HUB
  boot --> PR
  PR --> DC
  PR --> SW
  PR --> MO
  HUB --> TL
  HUB --> SW
  TL --> G12 --> SE --> RM --> LE
  SW --> Z --> DM
  DC --> Z --> DM
  LE --> POST --> CONF
  DM --> POST --> CONF
  DM --> VS
  DM --> TRIAGE
  LE --> TRIAGE
  TRIAGE --> TEL
```

### Role split (canary intent)

| Path | Role | Intended epics | Order path |
|------|------|----------------|------------|
| **B** | Primary high-frequency micro-scalper | EUR/USD + GBP/USD only (`forex_rotation_locked`) | Direct `place_market_order` + `confirm_deal` |
| **A** | Secondary macro entries | Night matrix (Gold, DOW, Nikkei, EUR/USD, …) | Full 12 gates → `LiveExecutor` |

Config reference: `config/config_v31_live_canary.json`.

---

## 3. Boot sequence (G1 → G5 → post-ready)

### 3.1 Gate progression

| Gate | Purpose | Canary notes |
|------|---------|--------------|
| G1 | Config, credentials, execution plane | Must load canary via `IG_AGENT_CONFIG` |
| G2 | REST auth, account hydration | Account **product type** (CFD vs SPREADBET) available here — critical for epic mapping |
| G3 | Quote stream ≤45s freshness | Observed transport: `yahoo` (not Lightstreamer on Mini) |
| G4 | OHLC + orchestrator (loops paused) | 7 loops built |
| G5 | READY, unpause loops, `start_post_ready_services` | Dual-core + stacked sweep armed |

**Observed runtime (2026-06-28):** `system_state.phase=G5`, `loops.running=true`, `accepting_ticks=true`, `fresh_count=7/7`.

### 3.2 Post-ready arming (`post_ready_services.py`)

Executed once at G5. Order matters for canary:

1. `start_dual_core_coordinator(rest, config)` — micro-scalper poll thread + executor pool  
2. `inject_session_unlimited_trades()` — clears session/daily trade caps; `order_cadence_sec=0`  
3. **`lock_forex_rotation_session()`** when `dual_core.forex_rotation_locked: true`  
4. **`reset_live_canary_session_gates(store)`** — baseline P&L, clear drawdown shield latches  
5. `start_stacked_dual_asset_tracks()` — 500ms async piercing sweep  
6. `start_socket_heartbeat_validator()` — stale quote rehydrate  
7. `start_virtual_stop_watchdog()` — 2pt internal ceiling on Path B fills  

Harness mode (`IG_TEST_HARNESS=1`) skips all of the above.

---

## 4. Path B — dual-core micro-scalper (detailed)

### 4.1 Signal sources (two converging paths)

**B-1 — Stacked 500ms sweep (primary hot path)**

```
execute_parallel_strategy_sweep (every 0.5s)
  → for each epic in get_active_stack_epics()
  → ingest_hub_mid → Z-score
  → if Z ≤ -2.00 (BUY) or Z ≥ +2.00 (SELL): dispatch_piercing_zone_order
  → _dispatch_micro_order
```

**B-2 — DualCoreCoordinator sync loop**

```
_loop() every poll interval
  → for snap in get_stacked_snapshots()
  → if snap.core_b_micro_active: _scan_micro_entries
  → evaluate_micro_scalp_signal (channel band touch)
  → _dispatch_micro_order
```

### 4.2 Mode routing (Z-score)

| Z range | Mode | Core B active |
|---------|------|---------------|
| Z ≥ 2.45 | `MACRO_BREAKOUT_SENTINEL` | No |
| Piercing / compressed | `LIGHTNING_MICRO_SCALPER` | **Yes** |
| Neutral band | `NEUTRAL` | No |

`CORE_B_FORCE_CHANNEL_OVERRIDE = True` arms micro mode across the full [-2, +2] band (clears “dead zone”), but **piercing dispatch still requires |Z| ≥ 2.0** in the sweep path.

### 4.3 Dispatch gate chain (`_dispatch_micro_order`)

Evaluated in order:

1. `is_strategy_kill_active()` → `BROKER_STATE_MISMATCH`  
2. `process_entry_blocked()` → QMM supervisor (kill switch, target achieved, spread/ATR circuit)  
3. `is_paused()` → `api_trading_paused`  
4. **`get_ig_position_sync().total_open() >= 1`** → `position_already_open`  
5. REST budget acquire (canary bypasses traffic governor; cap 12 tx/60s via separate path)  
6. `canary_lot_size()` → FX **1.0** lot  
7. **`resolve_account_product()` + `resolve_order_epic()`** → broker epic  
8. `place_market_order` → **`confirm_deal`** → triage ledger → `register_virtual_stop`  

Path B **does not** run: SignalEngine, 12 gates, RiskManager daily loss, entry_protection cooldowns, ML veto, environment fitness.

### 4.4 Forex lock (`forex_rotation_locked: true`)

- `lock_forex_rotation_session()` pins stack to hot-path pair.  
- `multi_source_auto_rotation_enabled()` → **False** (no rotation back to DOW/Gold).  
- `epic_allowed_on_hot_path()` rejects excluded epics.  
- Stagnant-quote rotation and high-velocity failover rotation **disabled** when locked.

**Wiring gap:** `lock_forex_rotation_session()` calls `resolve_hot_path_epics_from_config(cfg)` **without** `rest=`. When `broker_account_product` is `"auto"`, product detection defaults to **CFD** → stack set to `hot_path_epics_cfd_fallback` (`.CFD.IP` logical keys). This is correct for logical keys but depends on dispatch-time remapping to `.TODAY.IP` for spread-bet accounts.

### 4.5 Virtual stops

- Broker stop: stretched via `stretch_broker_stop_distance` (min ~2pt).  
- Internal 2pt ceiling: streaming mid + 500ms watchdog → flatten on breach.  
- **Only Path B fills** register virtual stops automatically.

---

## 5. Path A — 12-gate macro pipeline (detailed)

### 5.1 Per-tick flow

```
TradingLoop._run_tick
  → (optional) alpha matrix redirect [BYPASSED when live_canary.bypass_alpha_matrix]
  → quote integrity check
  → _evaluate_gates (cached ~10s except risk_validation every tick)
  → if all pass: ExecutionLoop.process_tick
  → SignalEngine.evaluate (entry_protection checks inside)
  → RiskManager.assess
  → LiveExecutor.execute → submit_atomic_entry → confirm_deal
```

### 5.2 Gate evaluation order (runtime)

Pre-gates (hard block before numbered gates):

- Master kill, QMM process block, entry circuit breaker  
- `active_rotation` soft block  
- Liquidity shield (>3.5× spread baseline)  
- Spread/ATR circuit breaker  

Then gates (note reorder vs dashboard labels):

1. `session_open`  
2. `session_blackout` (rollover 21:58–22:05 BST only for night matrix)  
3. `cold_start_gap`  
4. `environment_fitness` (floor 55% with protective_learning)  
5. **`points_state`** — STOP, session pause, daily loss, **drawdown shield**  
6. `correlation_ok`  
7. `signal_confidence` (floor 62% with protective_learning)  
8. `ml_veto`  
9. `risk_validation` (re-run every tick) — **max_open_positions: 1**  
10. `expectancy_ok`  
11. `calendar_ok`  
12. `execution` + REST rate limit  

### 5.3 Canary risk envelope (£5)

Multiple config keys align to £5:

- `max_daily_loss_gbp: 5.0`  
- `admin_safety_shield.daily_loss_limit_gbp: 5.0`  
- `learning_demo_mode.daily_loss_soft_pause_gbp: 5.0`  

`reset_live_canary_session_gates()` on boot baselines closed P&L and clears shield latch keys so stale demo history does not permanently block gate 5.

**Historical failure mode (pre-fix):** `effective_daily_pnl` from learning store showed large negative demo history → drawdown shield latched → **all epics blocked at gate 5 `points_state`**.

---

## 6. Shared infrastructure & cross-cutting blockers

### 6.1 Process-wide entry block (`qmm_process_supervisor`)

Single in-memory latch checked by **both** paths:

| Reason | Typical source |
|--------|----------------|
| `MASTER_KILL_SWITCH_ACTIVE` | Dashboard Stop / manual_stop.json |
| `BROKER_STATE_MISMATCH` | Strategy kill switch (ledger drift) |
| `TARGET_ACHIEVED_CAPITAL_PRESERVATION` | Target engine |
| `BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER` | Trading loop circuit |

### 6.2 REST layers

| Layer | Default | Canary |
|-------|---------|--------|
| Traffic governor | 3 POST/min | `bypass_traffic_governor: true` |
| Custom cap | — | `ig_rest_max_tx_per_60s: 12` |
| RestApiBudget | preemptive pause | shared |

### 6.3 Position sync

- Path A: `tracker.count_open_total() >= max_open_positions` in gate 9.  
- Path B: `ig_position_sync.total_open() >= 1` hard-coded.  
- Sync reads **live IG positions**, not triage REJECTED rows — rejected orders do **not** consume the slot.

---

## 7. Intended vs actual operation (live evidence)

### 7.1 What is working

| Component | Evidence |
|-----------|----------|
| Boot to G5 | Health API: phase G5, loops accepting ticks |
| Quote freshness | 7/7 assets fresh, age < 2s |
| Path B arming | `execution_mode=LIGHTNING_MICRO_SCALPER`, `core_b_micro_active=true` |
| Forex lock | `forex_rotation_locked=true`, stack `[EURUSD.CFD, GBPUSD.CFD]` |
| Order wire | 50+ POST attempts, confirm_deal returns structured reject payload |
| Triage ledger | Rows in `triage_v31.db::production_orders` with place+confirm JSON |
| Telemetry RTT | `broker_network_rtt_ms ≈ 297ms` |
| Session unlimited | Trade caps cleared at boot |

### 7.2 What is failing

| Component | Evidence | Impact |
|-----------|----------|--------|
| **Broker epic mapping** | Confirm raw: `"epic": "CS.D.GBPUSD.CFD.IP"`, reason `INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY` | **100% order rejection** |
| Account product auto-detect | CFD epic on wire despite canary config listing `.TODAY.IP` for spread bet | Spread-bet account cannot fill |
| Piercing frequency | `volatility_z_score ≈ 0.56` (needs \|Z\| ≥ 2) | Low dispatch rate except during vol spikes |
| Path A fills | No confirmed Path A deals in recent telemetry | Macro path not producing executions |
| Gate attribution API | Empty/minimal response in probe | Hard to see Path A WAIT breakdown from API alone |

### 7.3 Acceptance chain status

| Step | Path B (required) | Observed |
|------|-------------------|----------|
| Correct epics (EUR/GBP) | Yes | Logical epics correct; **broker epics wrong** |
| `place_market_order` | Yes | dealReference returned |
| `dealId` | Yes | Present on reject |
| `CONFIRMED` / fill | **Required** | **All REJECTED** |
| Triage row | Yes | Yes |
| Virtual stop armed | On fill only | Not armed (no fill) |

---

## 8. Logic battles — where subsystems fight each other

### 8.1 Dual trading planes, one position slot

```
Path B: total_open >= 1 → block
Path A: open_total >= max_open_positions (1) → block
Shared: single IG account
```

**Battle:** Path B can fire repeatedly on rejections (slot stays 0 on IG). Path A may simultaneously pass gates on a different epic and race for the same slot. There is **no mutex** between planes — only IG sync counts.

**Symptom:** Micro path hammers REST with rejects; macro path occasionally reaches execution but loses race or remains gate-blocked.

### 8.2 Epic universe mismatch

| Plane | Epic set |
|-------|----------|
| Path B (locked) | EUR/USD, GBP/USD |
| Path A (orchestrator) | 7 epics including DOW, Gold, Nikkei, Crude, FTSE, DAX |

Path A can still signal on indices/metals while Path B is forex-only — competing narratives for the single slot.

### 8.3 Gate stack asymmetry

Path B bypasses daily loss, ML veto, environment fitness, and entry_protection. Path A enforces all of them including **£5 shield**.

**Battle:** Operator expects “canary £5 envelope” to govern **all** trading; only Path A respects it. Path B can consume REST budget and churn rejects without incrementing meaningful risk state.

### 8.4 Confidence / fitness vs micro piercing

Protective learning floors (62% conf / 55% fitness) throttle Path A. Path B uses raw Z-score piercing independent of ML or fitness.

**Battle:** Telemetry shows `core_b_micro_active` while Path A on same epic would fail `signal_confidence` — two different “tradeable” definitions on the same quote.

### 8.5 Spread-bet config vs CFD runtime stack

Config documents:

```json
"hot_path_epics": ["CS.D.EURUSD.TODAY.IP", "CS.D.GBPUSD.TODAY.IP"],
"hot_path_epics_cfd_fallback": ["CS.D.EURUSD.CFD.IP", "CS.D.GBPUSD.CFD.IP"],
"broker_account_product": "auto"
```

Runtime:

- Active stack: **CFD.IP** (auto → CFD at boot without REST)  
- Wire epic on reject: **CFD.IP** (auto → CFD at dispatch)

**Battle:** Config *describes* spread-bet epics; resolver *emits* CFD epics. Comments in config say “set broker_account_product=CFD to use fallback” but auto-detection silently picks CFD fallback keys while still posting CFD codes to a spread-bet account.

### 8.6 Yahoo transport vs IG-native quotes

G3 complete with `transport: yahoo`. Micro channel and Z-score ingest Yahoo-sourced mids; broker executes on IG instruments.

**Battle:** Z-score piercing derived from Yahoo may not align with IG spread dynamics — causes signal timing skew (secondary to epic rejection).

### 8.7 Session unlimited vs £5 loss

`inject_session_unlimited_trades()` removes **count** limits. £5 loss gates remain on Path A only.

**Battle:** Unbounded reject retry loop on Path B (50+ attempts) without trade-count friction.

---

## 9. Feedback loops (operational)

### Loop 1 — Reject churn

```
Z pierce → dispatch → CFD epic → IG REJECT
  → triage row REJECTED → slot still 0 → dispatch again on next pierce
```

No exponential backoff on broker reject reason; no latch on `INSTRUMENT_NOT_TRADEABLE`.

### Loop 2 — Supervisor / boot kill (historical)

```
start.sh → pytest + supervisor
  stop.sh missed src/main.py pattern → zombie on :8080
  → health polls hang / false G5 on stale process
```

**Mitigation applied:** `stop.sh` now kills `src/main.py`. `start.sh` polls G5 with supervisor PID.

### Loop 3 — Drawdown shield (Path A, historical)

```
Stale learning P&L → shield latch → gate 5 WAIT all epics
  → operator sees “signals but no trades” on macro path only
```

**Mitigation applied:** `reset_live_canary_session_gates()` on boot.

### Loop 4 — Failover rotation deadlock (tests / edge runtime)

```
_activate_forex_failover → _rotate_active_stack_to
  → holds _lock → calls multi_source_auto_rotation_enabled() → deadlock
```

**Mitigation applied:** `_lock_held=True` parameter.

---

## 10. Gap register (prioritised fixes)

| ID | Severity | Gap | Fix direction |
|----|----------|-----|---------------|
| **G-E1** | **P0** | `resolve_account_product(auto)` returns CFD on spread-bet DEMO account | Set `dual_core.broker_account_product: "SPREADBET"` explicitly in canary config **or** fix `detect_account_product_from_rest` + pass `rest=` into `lock_forex_rotation_session()` |
| **G-E2** | **P0** | Confirm shows CFD epic on wire | Verify dispatch log line `broker_epic=CS.D.*.TODAY.IP`; add startup log of resolved account product |
| **G-E3** | P1 | No reject-reason circuit breaker | Latch dispatch pause on repeated `INSTRUMENT_NOT_TRADEABLE` (config epic mismatch) |
| **G-E4** | P1 | `lock_forex_rotation_session` resolves product without REST | `resolve_hot_path_epics_from_config(cfg, rest=rest_client)` at post-ready |
| **G-E5** | P2 | Path A/B epic competition | Disable Path A loops for non-hot epics in canary **or** raise `max_open_positions` with spec approval |
| **G-E6** | P2 | Piercing threshold rarely hit | Review Z window / ingest cadence; log pierce-near-miss at \|Z\| > 1.5 |
| **G-E7** | P2 | Path B skips £5 daily loss | Optional: call `RiskManager` pre-check in `_dispatch_micro_order` for canary parity |
| **G-E8** | P3 | Gate attribution API empty | Run `scripts/gate_attribution_report.py` against engine logs offline |
| **G-E9** | P3 | Virtual stops only on Path B | Document or unify stop policy for Path A fills |

---

## 11. Diagnostic playbook

### 11.1 Confirm broker product + epic (first step)

```bash
# Live telemetry — check last orders
curl -s http://127.0.0.1:8080/api/v31/telemetry | python3 -c "
import sys,json
d=json.load(sys.stdin)
for p in d.get('active_positions',[])[:5]:
    c=p.get('broker_payload',{}).get('confirm',{})
    print(p.get('epic'), p.get('status'), c.get('reason'), c.get('raw',{}).get('epic'))
"

# Engine log — broker epic at dispatch
grep -E 'broker_epic=|ForexRotationLock|live_canary:' src/data/v31-production/logs/supervisor.log | tail -30
```

**Expected after fix:** `broker_epic=CS.D.EURUSD.TODAY.IP` (or `.DAILY.IP`), confirm status `CONFIRMED` or `ACCEPTED`.

### 11.2 Path B acceptance grep

```bash
grep -E 'piercing dispatch|ENGINE_B_MICRO|micro order confirm|dealId=' \
  src/data/v31-production/logs/supervisor.log | tail -50
```

### 11.3 Path A gate holds

```bash
grep -E 'WAIT —|GATE_TRACE|points_state|drawdown|shield' \
  src/data/v31-production/logs/*.log | tail -80
# or
PYTHONPATH=src python3 scripts/gate_attribution_report.py
```

### 11.4 Process blocks

```bash
curl -s http://127.0.0.1:8080/api/v31/telemetry | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('block_reason', d.get('block_reason'))
print('suppression', d.get('last_gate_suppression_reason'))
"
```

---

## 12. Key source files

| Area | Path |
|------|------|
| Canary config | `config/config_v31_live_canary.json` |
| Boot / post-ready | `src/system/boot/post_ready_services.py`, `gate5_runner.py` |
| Path A gates | `src/trading/trading_loop.py` |
| Path A execution | `src/execution/live_executor.py`, `risk_manager.py` |
| Path B plane | `src/runtime/dual_core_execution.py` |
| Path B dispatch | `src/runtime/trade_manager.py` |
| Epic resolver | `src/execution/broker_epic_resolver.py` |
| Canary session reset | `src/runtime/live_canary_session.py` |
| Drawdown shield | `src/trading/manual_intervention.py` |
| Process block | `src/system/qmm_process_supervisor.py` |
| Telemetry | `src/api/v31_telemetry.py` |
| Ops | `scripts/start.sh`, `scripts/stop.sh`, `scripts/daemon_supervisor.sh` |
| Certification | `src/analytics/certification_report_v31.json` |

---

## 13. Recommended remediation sequence

1. ~~**Fix epic/product resolution (G-E1, G-E2, G-E4)**~~ **DONE** — `dual_core.broker_account_product: SPREADBET`; resolver reads nested config; hot path always logical CFD keys; dispatch maps to `.TODAY.IP`; boot logs account product.  
2. ~~**Add reject circuit breaker (G-E3)**~~ **DONE** — `runtime/broker_reject_guard.py` latches after 3 instrument rejections (15 min).  
3. ~~**Reduce plane competition (G-E5)**~~ **DONE** — Path A non-hot epics blocked via `live_canary_guards.canary_path_a_epic_allowed`.  
4. ~~**Path B £5 parity (G-E7)**~~ **DONE** — `canary_micro_dispatch_risk_ok` checks daily loss + shield before micro dispatch.  
5. **Verify one CONFIRMED fill on Path B** — restart `./scripts/stop.sh && ./scripts/start.sh`; grep `broker_epic=*.TODAY.IP` + `status=CONFIRMED`.  
6. **Path A soak** — optional macro fill on EUR/USD when signals pass 12 gates.  
7. **Update `certification_report_v31.json`** — mark Path B verification complete after live soak.

### Applied in codebase (2026-06-28)

| Fix | Module |
|-----|--------|
| SPREADBET product + nested config | `broker_epic_resolver.py`, `config_v31_live_canary.json` |
| Logical CFD hot stack | `resolve_hot_path_epics_from_config()` |
| Reject latch | `broker_reject_guard.py`, `trade_manager.py` |
| Path A epic scope | `live_canary_guards.py`, `trading_loop.py` |
| Path B risk parity | `live_canary_guards.py`, `trade_manager.py` |
| Virtual stop on reject | skip register when `status=REJECTED` |
| Test gate expanded | `start.sh` — 46 tests across 8 files |

---

## Appendix A — Config quick reference (canary)

| Key | Value | Notes |
|-----|-------|-------|
| `live_canary.enabled` | true | Baseline reset |
| `max_open_positions` | 1 | Shared slot |
| `max_daily_loss_gbp` | 5.0 | Path A |
| `dual_core.forex_rotation_locked` | true | EUR/GBP only |
| `dual_core.broker_account_product` | **auto** | **Suspect — set SPREADBET if account is spread-bet** |
| `execution.order_cadence_sec` | 0 | Unlimited micro cadence |
| `protective_learning` floors | 62 / 55 | Path A only |

---

## Appendix B — Runtime snapshot (authoring)

```
phase: G5
execution_mode: LIGHTNING_MICRO_SCALPER
forex_rotation_locked: true
active_stack: [CS.D.EURUSD.CFD.IP, CS.D.GBPUSD.CFD.IP]
volatility_z_score: ~0.56 (below pierce threshold 2.0)
production_orders: 50 REJECTED — INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY
block_reason: (empty)
last_gate_suppression_reason: (empty)
broker_network_rtt_ms: ~297
```

This snapshot confirms the system is **trying to trade** but IG is **rejecting every order** due to instrument/currency mismatch — not due to absence of signals or dispatch logic.
