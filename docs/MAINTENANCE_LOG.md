# Maintenance Lockdown — E2E State-Machine Validation Log

**Status:** Maintenance lockdown active — live operations halted  
**Suite version:** v29.1 stress framework  
**Last updated:** 2026-06-17 (Capital Harvesting + Infinite Edge + Co-Pilot Phase 2)

---

## Core architectural rule — two-decimal broker lot contract

**Permanent fix for the 1.125 block error** (risk-band scaling: `1.5 × 0.75 → 1.125`).

| Rule | Implementation |
|------|----------------|
| Truncation | `truncate_to_broker_lot(size)` in `trading/position_ladder.py` — `math.floor(size × 100) / 100` |
| Pre-dispatch weld | `apply_broker_lot_contract()`, `finalize_dispatch_lot_size()`, `weld_execution_params_lot()` |
| Execution routers | `execution/rocket_trigger.py` → `weld_rocket_dispatch_params()`, `weld_rest_payload_map()` |
| Operational floors | `execution/size_floors.py` → `apply_operational_size_floor()` delegates to broker contract |
| REST dispatch | `execution/live_executor.py` — final payload weld before `IGRestClient.place_market_order` |

**Contract:** Any fraction like `1.125` MUST evaluate to `1.12` before IG REST. Three-decimal lots are forbidden.

**S2b sub-gate:** `capacity_throttle.run_lot_boundary_gate()` simulates 5,000 risk-scaling events; raises `LotSizeIntegrityError` on any >2-decimal lot.

---

## Purpose

This document records the monolithic E2E state-machine validation and stress-testing
engine deployed during maintenance lockdown. The goal is to mathematically and
operationally prove system stability across all market environments before
safe re-deployment.

---

## Suite layout

```
src/stress/
  __init__.py              # Package exports
  time_controller.py       # VirtualClock — UK time-travel without host clock mutation
  historical_feed.py       # 5-decimal scenario tick generator (trend / flash / chop / gap)
  telemetry_packet.py      # SchemaDriftTracker + TelemetryPacketGenerator

tests/stress/
  __init__.py
  scenario_replayer.py     # Gate S1 — microstructure + ATR trail regression
  capacity_throttle.py     # Gate S2 — telemetry flood + WebSocket soak
  regional_lifecycle.py    # Gate S3 — Tokyo / London / rollover / drawdown contracts
```

---

## State-machine test gates

| Gate | Module | Proves |
|------|--------|--------|
| **S1** | `scenario_replayer.py` | NumPy microstructure classifies replayed ticks; `AlphaOptimisedTrailEngine` ATR multiples scale fluidly with `risk_compression_factor`; no placeholder prices or integer-truncated trail multiples |
| **S2** | `capacity_throttle.py` | **Max load 5,000 frames/sec** — Pydantic telemetry + **S2b lot boundary** (no size >2 decimals); duplicate `seq`/`dealId` → `CapacityIntegrityError`; queue burst + WebSocket soak |
| **S3** | `regional_lifecycle.py` | Tokyo 01:00–04:00 UK Nikkei 65% floor + 0.5× lot scale; defensive epics 85%; London 08:00–10:00 +35% ML confidence (`×1.35`) + 1.5× autopilot slot multiplier; rollover 21:58–22:05 BST blocks all night-matrix routing; −£500 Superjet drawdown triggers flatten + `manual_stop` freeze |

---

## Verified architectural constraints

### Tokyo window (01:00–04:00 UK)

- **Module:** `intelligence/liquidity_wave.py`
- Nikkei (`IX.D.NIKKEI.IFM.IP`): microstructure floor **65%**
- Gold / Wall St / EUR/USD: remain **85%**
- Micro confidence 65–84%: **0.5×** position size via `overnight_volatility_size_multiplier()`

### London open (08:00–10:00 UK)

- **Module:** `intelligence/liquidity_wave.py` → `LiquidityPhase.LONDON_OPEN`
- ML confidence injection: **`confidence_multiplier = 1.35`** (+35%)
- Position slot expansion: **`autopilot_multiplier = 1.5`**
- Applied through `apply_microstructure_wave()` and `effective_autopilot_max_per_epic()`

### Daily rollover lock (21:58–22:05 BST)

- **Module:** `intelligence/premium_overnight.py`
- `night_matrix_session_allowed()` returns blocked during lock
- `check_session_blackout()` surfaces `rollover lock 21:58-22:05 BST`
- Clears at **22:05** — no legacy 20:00–06:00 weekday blackout for night matrix

### Drawdown circuit breaker (−£500)

- **Module:** `system/superjet_drawdown_guard.py`
- `MAX_DAILY_DRAWDOWN_GBP = 500.0` (additive ceiling; does not replace `max_daily_loss_gbp`)
- Breach path: `check_and_enforce_async()` → emergency flatten → `mark_manual_stop(source=superjet_drawdown_ceiling)` → freeze until UK midnight

### Telemetry schema contract

- **Module:** `cockpit/telemetry_schema.py` — `IgPositionTelemetry`
- Required fields: `dealId`, `entry`/`level` (5-decimal FX), `profitAndLoss`
- Placeholders (`—`, `N/A`, empty) rejected at ingress

### Institutional Capital Harvesting Contract

- **Module:** `intelligence/alpha_trail.py` → `apply_capital_harvest_contract()`
- **Anti-Regret BE:** +15 pips → stop at entry + 1.5 pips
- **2R Lock:** profit ≥ 2R → stop at entry + 1R
- **Parabolic Snap:** P_day ≥ 75% of £1k → lock 50% float / £500 floor (`target_engine.py`)
- **Logging:** `CAPITAL_HARVEST [trigger]` → `engine.log` → Flight Deck avionics log
- **Tests:** `tests/test_intelligence_alpha_trail.py`

### Flight Deck co-pilot telemetry

- **Modules:** `cockpit/telemetry_bridge.py`, `cockpit-web/`
- Fields: `global_ai_status_key`, `market_states_map`, `macro_radar`, `shadow_trading`, `order_book_imbalance`
- WebSockets: `/ws/telemetry`, `/ws/logs`, `/ws/triage` @ 2.5 Hz

### Infinite Edge plane

- `intelligence/macro_radar.py` — non-blocking DXY/10Y proxy collector
- `intelligence/microstructure.py` — velocity ENGAGED disables RSI≤85; REST→live tick blend
- `trading/shadow_executor.py` — `IG_AGENT_MODE=SHADOW`
- `system/thread_affinity.py` — P-core / QoS thread pinning

---

## Bottlenecks under maximum throttle

| Area | Observation | Mitigation |
|------|-------------|------------|
| Telemetry queue | `maxsize=32` — burst >32 frames drops oldest via `put_drop_oldest` | By design for non-blocking producers; S2 measures drop count |
| WebSocket publish rate | Cockpit server hard-coded **2.5 Hz** (`web_server.py`) | S2 schema flood validates Pydantic path at 2k–5k/s independently of WS cadence |
| REST budget | 3 calls/min hard cap unrelated to stress suite | Stress tests use isolated replay — no IG REST |
| Drawdown enforce | Async worker thread — 3s wait in S3 test | Production flatten depends on `cockpit.emergency` + IG REST availability |

---

## How to run (maintenance lockdown)

**Prerequisite:** Agent stopped, `manual_stop` active, no open positions.

```bash
cd /Users/chrisgordon/Projects/IG_Agent_v25

# Full stress gate (all three suites)
PYTHONPATH=src python3 -m pytest tests/stress/ -v

# Individual gates
PYTHONPATH=src python3 -m pytest tests/stress/scenario_replayer.py -v
PYTHONPATH=src python3 -m pytest tests/stress/capacity_throttle.py -v
PYTHONPATH=src python3 -m pytest tests/stress/regional_lifecycle.py -v

# Maximum throttle schema flood (optional — 5000 frames)
STRESS_RATE=5000 STRESS_FRAMES=5000 PYTHONPATH=src python3 tests/stress/capacity_throttle.py
```

**100% pass criteria:**

- S1: all `ScenarioReplayerTests` green; no placeholder stops; ATR multiples ∈ [0.1, 2.0]
- S2: no `CapacityIntegrityError` / `LotSizeIntegrityError`; **5,000-frame** schema flood + lot gate; `peak_rss_mb < 64` on queue burst; WebSocket soak ≥8 frames
- S3: Tokyo/London/rollover/drawdown contracts asserted; `is_frozen()` after −£500 breach

---

## Clean-slate re-deployment guide

Once all stress gates pass:

1. **Confirm lockdown clear**
   ```bash
   pgrep -f src/main.py || echo "agent stopped"
   test -f src/data/state/manual_stop.json && cat src/data/state/manual_stop.json
   ```

2. **Run full regression stack**
   ```bash
   PYTHONPATH=src python3 -m pytest tests/stress/ tests/test_liquidity_wave.py tests/test_premium_overnight.py tests/test_superjet_hud.py tests/test_telemetry_schema.py tests/test_intelligence_alpha_trail.py tests/test_infinite_edge_overhaul.py tests/test_cockpit_avionics.py -q
   ```

3. **Pre-flight (DEMO)**
   ```bash
   PYTHONPATH=src python3 scripts/pre_flight_check.py
   ```

4. **Desktop launch** — `IG Agent Flight Deck.app`  
   - Verify Gate2 auth on **first** attempt (`.env` override fix in `main.py` for `IG_AGENT_FROM_LAUNCHER=1`)
   - Confirm Flight Deck `:8787` + dashboard `:8080`

5. **Post-launch smoke** — wait for first 5m bar close; confirm `ALL GATES PASSED` → `Order confirmed` → learning DB row

6. **Remove maintenance hold** — clear `manual_stop` only after operator sign-off:
   ```bash
   rm -f src/data/state/manual_stop.json
   ```

---

## Files added in this maintenance cycle

| Path | Role |
|------|------|
| `src/stress/time_controller.py` | Virtual UK clock |
| `src/stress/historical_feed.py` | Scenario tick playback |
| `src/stress/telemetry_packet.py` | Flood generator + integrity tracker |
| `tests/stress/scenario_replayer.py` | S1 regression engine |
| `src/trading/position_ladder.py` | `truncate_to_broker_lot` + pre-dispatch weld |
| `src/execution/rocket_trigger.py` | Rocket/REST payload lot weld |
| `tests/stress/capacity_throttle.py` | S2 + S2b max-load (5k) profile |
| `tests/stress/regional_lifecycle.py` | S3 regional contracts |
| `src/intelligence/alpha_trail.py` | Capital Harvesting contract |
| `src/trading/alpha_trail.py` | Trading-plane harvest facade |
| `src/intelligence/macro_radar.py` | Cross-asset macro correlation |
| `src/trading/shadow_executor.py` | Shadow mode ledger |
| `src/system/thread_affinity.py` | M-series P-core pinning |
| `cockpit-web/` | Flight Deck static UI |
| `src/cockpit/` | Flight Deck backend + telemetry bridge |
| `tests/test_infinite_edge_overhaul.py` | Infinite edge regression |
| `docs/MAINTENANCE_LOG.md` | This document |

---

## Known non-blocking issues (pre-existing)

- `UnboundLocalError` in IG transaction sync at boot (logged, non-fatal)
- Wall Street `FLOOR_EXCEEDS_CAP` when stop distance × size exceeds £150 operational cap
- `confirm_started.py` requires authenticated `/api/health` — use `/health` for quick check

---

*End of maintenance log — do not re-arm live trading until S1–S3 are 100% green.*

---

## 2026-07-03 19:22 BST — Incident status: packet-validator circuit breaker fix HOLDING

**Incident:** Market data hub reported `fresh=0` for hours; the packet-validator feed
circuit breaker re-tripped every 5 minutes (`malformed_rate=99.9%`), rejecting ~100% of
real quotes. Root cause: stale hardcoded `_HUB_SEED_DEFAULTS` (2024-era prices, 30–80%
below live levels) poisoned the validator's out-of-order anchor, which was never updated
on rejection; breaker drops were also counted as malformed traffic, so the breaker
re-tripped the instant it expired.

**Fix (live since ~19:03 BST restart):**
- `src/system/packet_validator.py` — re-anchor last-mid after 3 consecutive jump rejects;
  circuit-breaker drops no longer count toward the malformed-rate window.
- `src/system/market_data_hub.py` — `_HUB_SEED_DEFAULTS` updated to current price levels.
- `src/data/ohlc_yahoo_seeder.py` — crude spread 0.04; generic fallback spread scales
  with price (flat 15.0 was 22% of crude mid → tripped the 10% spread cap).

**Verified 19:22 BST:**
- Last circuit-breaker trip: **19:02:00** (old process, during shutdown). None since —
  previously it tripped every 5 minutes on the dot.
- Feeds: `quotes_fresh=True`, **7/7 epics fresh**, `trading_healthy=True`.
- Throughput actively flowing: ~830 log lines / 2 min; sweep evaluating live z-scores
  (piercing-zone detections on Dow/Nikkei); last log line current to the second.
- Process check clean: no dangling pytest; exactly one `src/main.py` (pid 97789,
  ~30% CPU / 179MB RSS, normal) under `daemon_supervisor.sh` (pid 9525); only child is
  the standard multiprocessing resource-tracker.

**Note on recent task notifications:** the batch of "aborted"/completed shell task alerts
around 19:12 were stale pre-fix test batches plus two deliberately killed wedged pytest
runs (they hung on live network calls during the agent restart). Safe to ignore.

---

## 2026-07-03 20:15 BST — Quant audit fixes deployed (protocol restart)

**Scope:** Full audit remediation across 12 modules — look-ahead/self-inclusion bias,
hot-path bottlenecks, backtest/ML holdouts, matrix backtuner hindsight fixes.

**Restart:** manual_stop hold → TERM (pid 97789) → pycache purge → supervisor relaunch.
New agent **pid 61512**, port 8080 bound ~20:14 BST.

**Verified 20:15 BST:**
- `quotes_fresh=True`, **7/7 epics fresh**, `trading_healthy=True`, `trading_loops_running=True`
- No packet-validator circuit-breaker trip since restart (prior recurring 5-minute cycle absent)
- Transient boot heal on execution (`sweep_stalled`) during Gate F warm-up — resolved as loops armed

**Key live-path changes now active:**
- Prior-window z-scores in `dual_core_execution` (entry-gating signal no longer self-damped)
- Leave-one-out spread/sweep z-scores in intelligence plane
- Regime ATR ratio baseline excludes current bar; indicator memo cache on ring revision
- Signal engine single-pass multi-timeframe resampling

---

## 2026-07-03 20:28 BST — Indicator kernel refinement deployed (protocol restart)

**Scope:** `src/signals/indicators.py` only — anti-curve-fit constants, lag-reduced direction
deadbands, vectorized EMA/RoC/percentile helpers, flow-boost cap at 25%.

**Restart:** manual_stop hold → TERM (pid 61512) → pycache purge → supervisor relaunch.
New agent **pid 74366**, port 8080 bound ~20:28 BST.

**Verified 20:28 BST:**
- `quotes_fresh=True`, **7/7 epics fresh**, `trading_healthy=True`
- No packet-validator circuit-breaker trip since prior fix (~19:02 last trip)
- Indicator tests: 20/20 pass (`test_apex_indicators`, `test_predictive_microkernel`,
  `test_adversarial_hardening::MicroTrendAlphaTests`)
