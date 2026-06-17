# Intelligence Layer Blueprint (v29.1)

Architectural specification for the next-generation intelligence plugins that
extend the unified QMM execution pipeline **without** modifying the boot
coordinator or `agent_bootstrap` initialization sequence.

---

## Design principles

| Principle | Implementation |
|-----------|----------------|
| **Modular plugins** | `src/intelligence/` — three pure-math engines + worker + bridge |
| **Non-blocking ingress** | Hub callbacks call `enqueue_tick()` only (O(1) deque append) |
| **Heavy compute isolation** | `IntelligenceComputeWorker` daemon thread (250ms cadence) |
| **Pipeline integration** | `IntelligenceLayer` façade via `pipeline_bridge.py` — bind later |
| **No boot changes** | `wire_intelligence_to_hub()` is opt-in from Gate 3 / post-ready |

---

## Package map

```
src/intelligence/
├── types.py                 # Verdict dataclasses
├── spread_forecast.py       # Dynamic Spread-Widening Forecast Model
├── microstructure.py        # Multi-Timeframe Micro-Structure Classifier
├── alpha_trail.py           # Alpha-Optimised Trailing Engine
├── intelligence_worker.py   # Background compute + hub wire helper
├── pipeline_bridge.py       # Plugin façade for gates / execution router
└── __init__.py
```

---

## 1. Dynamic Spread-Widening Forecast Model

**Purpose:** Detect and predict IG spread widening during news / low-liquidity windows.

**Class:** `SpreadWideningForecast`

**Algorithm:**
- Rolling window (default 120 ticks) of absolute spread per epic
- Z-score of current spread vs window mean/std
- Z-score of spread **delta** (tick-over-tick widening)
- Breach when `z ≥ 2.5` or `delta_z ≥ 2.0` with positive delta

**Outputs (`SpreadForecastVerdict`):**
- `throttle_factor` ∈ [0, 0.85] — scales execution size / frequency
- `offset_widen_pts` — widen stop/limit offsets to absorb slippage
- `blocked` — hard gate when severity ≥ 1.8× threshold

**Pipeline bind (future):**
```python
layer = get_intelligence_layer()
adj = layer.execution_adjustments(epic)
if adj["intelligence_spread_blocked"]:
    # skip entry or defer to OrderConfirmWorker
```

---

## 2. Multi-Timeframe Micro-Structure Classifier

**Purpose:** Classify short-term order flow from Lightstreamer ticks.

**Class:** `MicrostructureClassifier`

**Timeframes:** 5s, 1m, 5m ring buffers (numpy rolling features)

**Features per window:**
- Momentum slope (linear regression on mid price)
- Return volatility (std of tick returns)
- Sweep score = max |return| / σ
- Order block = 1m compression + 5s breakout

**Regimes:** `NEUTRAL`, `MOMENTUM_UP`, `MOMENTUM_DOWN`, `SWEEP_BUY`, `SWEEP_SELL`, `ORDER_BLOCK`

**Pipeline bind (future):**
- Gate 10/11 sizing multiplier modulation via `micro.regime`
- Correlation guard soft-block on opposing sweep

---

## 3. Alpha-Optimised Trailing Engine

**Purpose:** £1,000/day scalping cadence — lock profits aggressively, room in trends.

**Class:** `AlphaOptimisedTrailEngine`

**Mechanism:**
- Base trail distance = `ATR × multiple`
- Multiple widens in momentum regimes (`run_atr_mult=0.85`)
- Multiple tightens after profit ≥ 12 pts or session milestone ≥ 40 pts
- Delegates stop proposal to `eval_trailing_stop()` (existing hot-path math)

**Pipeline bind (future):**
- Called from `position_protect_hub` fast path with micro regime context
- Complements (does not replace) `trailing_stop_engine.py`

---

## Worker architecture

```
Lightstreamer / REST poll
        │
        ▼
  MarketDataHub.publish()
        │
        ├─► position_protect_hub (existing, ~50ms)
        │
        └─► wire_intelligence_to_hub()  [opt-in]
                 │
                 ▼
        IntelligenceComputeWorker.enqueue_tick()  ← O(1), non-blocking
                 │
                 ▼ (250ms daemon)
        SpreadWideningForecast.compute()
        MicrostructureClassifier.classify()
                 │
                 ▼
        IntelligenceSnapshot cache  ← read by IntelligenceLayer
```

**Overlap guard:** `SyncTaskGuard("IntelligenceComputeWorker")`

---

## Integration checklist (Phase 5 — not yet wired)

1. Enable in config overlay: `intelligence_layer.enabled: true`
2. Call `wire_intelligence_to_hub()` from `gate3_runner.py` (post-hub subscribers)
3. Call `start_intelligence_worker()` from `post_ready_services.py`
4. Merge `execution_adjustments()` in `execution_engine.py` pre-dispatch
5. Pass `micro.regime` into `AlphaOptimisedTrailEngine` from `trade_manager` fast path

---

## Tests

| File | Coverage |
|------|----------|
| `tests/test_intelligence_spread_forecast.py` | Z-score breach, throttle, mock widening series |
| `tests/test_intelligence_microstructure.py` | Sweep, momentum, order-block mock ticks |
| `tests/test_intelligence_alpha_trail.py` | ATR trail tighten/loosen, eval_trailing_stop delegation |
| `tests/test_intelligence_worker.py` | Non-blocking enqueue + background compute |

Run:
```bash
PYTHONPATH=src python3 -m pytest tests/test_intelligence_*.py -q
```
