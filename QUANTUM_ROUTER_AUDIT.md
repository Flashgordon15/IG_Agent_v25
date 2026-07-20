# QUANTUM ROUTER AUDIT — Multi-Market Deployment Blueprint

**Repo:** `IG_Agent_v25` · **Runtime:** v31.1 DEMO Trading Desk · **Host:** Mac Mini (rest_poll / Yahoo)  
**Config authority:** `config/config_v31_demo_throughput.json` (extends v31 → v30 → v29 → v25)  
**Data root:** `src/data/v31-production/` via `IG_DATA_ROOT` / `data_dir()`  
**Product UI:** Quantum Terminal `:3000` (`Trading_Desk.app`) — not Flight Deck `:8787`

---

## 0. Operator Audit Snapshot (read-only)

Before any hot-reload or deploy:

| Gate | Source of truth |
|------|-----------------|
| Flat book? | `GET /api/positions/live` → `verdict=FLAT`, `broker_open_sot.count=0` |
| Trade support | `GET /api/trade_support/status` |
| Hold? | deploy-hold markers under data root / `mark_manual_stop` |
| Anti-zombie | `mark_manual_stop` → `TERM` main → wait port 8080 free → purge `__pycache__` |

**Do not** `kill -9` `main.py`. Night-matrix blackout 20:00–06:00 BST is **deleted**; sole scheduled block = rollover **21:58–22:05 BST**.

---

## 1. Core Blueprint & Operation Manual

### 1.1 End-to-end lane map

```
Yahoo / rest_poll (Mini) ──┐
IG REST market snapshots ──┤→ MarketDataHub → DualCore 500ms sweep
Lightstreamer (design) ────┘         │
                                     ├→ TradingLoop / gated entry
                                     ├→ MemoryContext (RAM opens)
                                     ├→ micro_gbp_exit + risk stack
                                     └→ Fulfillment cache → WS → Quantum Terminal
                                                                  ├─ GPUExecutionCanvas
                                                                  ├─ AIMarketScanner
                                                                  └─ DeskAlphaStrip / why_idle
```

| Lane | Module(s) | Responsibility |
|------|-----------|----------------|
| **Ingestion** | `src/system/market_data_hub.py`, `src/system/feeds/multi_feed_hub.py`, `feeder/yahoo_quote_poller.py` | Quote authority into hub snapshots |
| **Integrity** | `src/system/market_integrity.py` | Transport-aware quote age + market-open gates |
| **Rotation** | `src/runtime/dual_core_execution.py` | 500ms multi-source stack sweep; `exclude_from_hot_path` |
| **Hot path RAM** | `src/system/memory_context.py` | Thread-safe slotted open matrix; hollow-ghost veto |
| **Bootstrap** | `src/system/runtime_context.py` | Script/harness REST + liquid epic pick (not tick lane) |
| **Entry veto** | `src/execution/pre_entry_regime_veto.py`, `entry_gate_hardening.py` | Spread %/pts, OBI ±0.15, elasticity → WO |
| **Accounting** | `src/diagnostics/performance_journal.py` | Non-blocking WIN/LOSS GBP CSV ledger |
| **Open P&L** | `src/execution/position_pnl_gbp.py`, `src/trading/open_position_view.py` | Broker UPL → account GBP |
| **FE** | `terminal/src/components/gpu/GPUExecutionCanvas.tsx` | RAF GPU risk canvas on `:3000` |

### 1.2 Ingestion — Lightstreamer design vs Mac Mini truth

**Design (Lightstreamer / event-driven WS):**

- Hot-path quote budget: `LIVE_QUOTE_MAX_AGE_SEC = 0.5` in `market_integrity.py`.
- Intended for sub-second sniper arming when IG streaming is healthy.
- `event_driven_tick` in demo config documents a raw WS path with no TWMA coalesce.

**Mac Mini actual transport (authoritative today):**

- `config/config_v25.json`: `"streaming_transport": "rest_poll"` (inherited through merge chain).
- Demo overlay: `pricing.reference_transport = "yahoo"`, `yahoo_poll_sec = 2.0`.
- `streaming_transport_is_rest_poll()` returns true for `rest_poll` / `yahoo` / poll modes **or** when Yahoo is the reference transport.
- Entry / sniper budget under rest_poll: `effective_entry_quote_budget_sec()` → `feed_quality.entry_veto_age_sec` (default **10s**), clamped to `[0.5, 45]`.
- **Never clamp rest_poll down to 500ms** — that historically caused permanent fail-closed entries / ASSET IDLE (see `unified_fulfillment_cache.py` comments around quote freshness).

**Operator truth:** Mini streams Yahoo+REST poll into `MarketDataHub`; Lightstreamer remains in the architecture but is not the live Mini transport.

### 1.3 Hot path — `MemoryContext` slotted matrix

File: `src/system/memory_context.py`

- `OpenPositionMem` uses `__slots__` for dense RAM packing: `deal_id`, `epic`, `direction`, `size`, `entry`, `pnl_gbp`, `soft_loss_gbp`, `trail_floor_gbp`, `target_gbp`, `peak_profit_gbp`, `atr`, `take_profit_level`, …
- `MemoryContext` is a process singleton with `threading.RLock` — **zero disk I/O** on the hot path.
- `sync_open_rows()` hard-vetoes hollow ghosts (`entry<=0` or overlay null-PnL stubs).
- Quote freshness on the matrix uses `effective_entry_quote_budget_sec()` (transport-aware), not a hard-coded 500ms on Mini.
- ATR take-profit attachment: `ATR_TP_MULT = 3.5`.

### 1.4 Accounting — performance journal (WIN/LOSS GBP)

File: `src/diagnostics/performance_journal.py`

- Hot path only `queue.put_nowait` close/flat events.
- Daemon worker appends CSV to `src/data/v31-production/metrics/daily_journal.csv`:
  `Timestamp,DealID,Direction,EntryPrice,ExitPrice,RealizedPnL_GBP,ClosingFillRate,ActiveSlipMultiplier`
- Benchmark row: `BENCHMARK_OFFSET:£1000_DAILY` / `DAILY_MILESTONE_GBP = 1000.0`.
- Intentionally **off** the Lightstreamer/tick lane — fill-rate hooks run at write time only.

### 1.5 FE — `GPUExecutionCanvas` on `:3000`

File: `terminal/src/components/gpu/GPUExecutionCanvas.tsx`

- Hardware RAF loop over mutable `GpuExecutionBuffer` (no per-tick React re-renders).
- Renders soft_loss_gbp / trail_floor_gbp / focus epic risk plane.
- Shell: `GpuPlatformShell` → `AIMarketScanner` + fleet panels; multiplexed via `useQuantumNodeMemory` / desk WS.

---

## 2. Multi-Market Rotation Stubs — Why DOW-Only

### 2.1 Config block (authoritative)

From `config/config_v31_demo_throughput.json` → `dual_core`:

```json
"dual_core": {
  "active_stack_slots": 2,
  "multi_source_auto_rotation": true,
  "exclude_from_hot_path": [
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.GBPUSD.CFD.IP",
    "IX.D.DAX.IFM.IP",
    "IX.D.FTSE.IFM.IP",
    "CS.D.CRUDE.CFD.IP",
    "IX.D.NIKKEI.IFM.IP"
  ]
}
```

**Net effect:** hot-path entries = **DOW only** (`IX.D.DOW.IFM.IP`). Nikkei stays excluded until JPY PnL valuation is certified at a flat deploy window. Night-matrix epics may still **stream**; dispatch follows `exclude_from_hot_path` + `epic_allowed_on_hot_path()`.

Enforcement sites:

- `dual_core_execution.py` — tradeable stack build skips excluded epics (~L179).
- `epic_allowed_on_hot_path()` — reject dispatch outside stack / exclude list.
- Session rule (`.cursor/rules/2026-07-07-trading-desk-session.mdc`): DOW hot path until Nikkei JPY PnL certified.

### 2.2 Currency P&L / soft_loss / micro-trail gaps

| Concern | Where | Gap |
|---------|-------|-----|
| Instrument specs | `open_position_view.INSTRUMENT_PNL_SPEC` | DOW (USD), Nikkei (JPY), Gold (USD), DAX (EUR) present; **FTSE absent** → defaults `point_value=1.0, currency=GBP` |
| FX pips | `pnl_math.pip_size_for_epic` + `fx_upl_per_ig_point` | EUR/USD etc. use pip math + ~$10/pip/unit; UPL currency USD → `pnl_currency_amount_to_gbp` |
| Soft loss | `micro_gbp_exit.register_gbp_exit` | Soft = `loss_cap_gbp * soft_loss_ratio` (config `micro_risk.soft_loss_ratio=0.55`) — **GBP-denominated**, not per-microstructure |
| Point risk sizing | `trading_loop` `ig_point_value_gbp` + USD→GBP | Index-centric; FX/Gold need broker UPL path, not raw index £/pt |
| Gold / FTSE floor | Canary sizes in `strategy_quality.canary_size_by_epic` | Gold 10.0 £/pt floor exists; FTSE not in canary map; soft/trail noise filters not per-epic |

**Certification blockers before multi-asset live entries:**

1. Nikkei: JPY→GBP soft_loss / trail floors must match broker UPL (not synthetic index points).
2. EUR/USD: pip→GBP path certified under spreadbet/CFD product plane in use.
3. Gold: USD point value + 10.0 £/pt size floor + wider spread budget.
4. FTSE: add to `INSTRUMENT_PNL_SPEC` + per-epic `max_spread_pts` + soft_loss noise filter.

### 2.3 Entry gates — false positives on FX / commodities

**`pre_entry_regime_veto.py`**

- `DEFAULT_MAX_SPREAD_PCT = 0.0002` (0.02% of mid).
- Config: `pre_entry_regime_veto.max_spread_pct=0.0002`, `max_spread_pts=3.0`.
- Hard `BLOCK` when `spread_pts > 3.0` or spread% exceeds 0.02%.

**`entry_gate_hardening.py`**

- `feed_quality.max_spread_pts=3.0` + `spread_hard_veto=true` — global pts ceiling.
- OBI filter: `obi_filter.min_abs_ratio=0.15`, `require_align=true` (never BUY into OBI≤−0.15 / SELL into melt-up).

**Why this false-positives FX/Gold:**

- Index DOW spreads often fit ≤3 pts; Gold and some FX quotes routinely print wider absolute spreads.
- Dual-core channel health has saner per-epic defaults (`_DEFAULT_MAX_SPREAD_PTS`: DOW 12, EURUSD 3, FTSE 12, Crude 10) — but **entry hardening still uses the global 3.0** from `feed_quality` / regime veto.
- Expansion must move to **per-epic spread ceilings** before removing assets from `exclude_from_hot_path`.

---

## 3. ASSET IDLE / SCANNING ALTERNATIVE SNIPER PROXY

### 3.1 Exact UI copy path

Literal string constructed in:

**`terminal/src/hooks/useQuantumNodeMemory.ts`** → `buildScanner()`

```ts
const proxyTag = "FX_EURUSD";
// default + idle branch:
statusText = `STATUS: ASSET IDLE. SCANNING ALTERNATIVE SNIPER PROXY -> [${proxyTag}]`;
```

Rendered by **`terminal/src/components/gpu/AIMarketScanner.tsx`** (`row.statusText`).

Scanner targets (UI only): DOW, DAX, GOLD, BRENT — not the full night matrix.

Idle reasons encoded in `profile` (not the STATUS line): `VOL_VETO` | `RANGE_BOUND` | `STAGNANT_DZ` | `ENTRIES_GATED` | `ROTATION_IDLE`.

**Sniper arm conditions** (all required): not chop, not vol veto, not stagnant dead-zone, `allowEntries`, `inActiveStack`, `quotesFresh`, `fulfillment.all_ready`, not `trading_paused`.

Because `exclude_from_hot_path` keeps non-DOW off the active stack, DAX/GOLD/BRENT almost always land in the IDLE + proxy copy even when quotes stream.

### 3.2 Backend: alt quotes vs age clamp

| Layer | Behaviour |
|-------|-----------|
| Hub / Yahoo | Continues to poll multi-epic quotes (fulfillment stages, rotation scores). |
| Dual-core | 500ms sweep scores universe; excluded epics never enter tradeable hot stack. |
| Fulfillment | `unified_fulfillment_cache`: sniper `all_ready` uses **transport-aware** hub freshness; multi-feed velocity must not veto a healthy rest_poll hub (historical permanent ASSET IDLE bug). |
| why_idle API | `GET /api/desk/why_idle` → `desk_self_assess.build_why_idle_payload()` using `effective_entry_quote_budget_sec()`. |
| DeskAlphaStrip | FE calls `/api/desk/why_idle?heal=true` for operator heal path. |

**Root cause of the IDLE copy (current desk):** primarily **rotation / stack membership** (`!inActiveStack` → `ROTATION_IDLE`) + regime gates — **not** a 500ms live clamp on Mini. Mini budget is ~**10s** via `effective_entry_quote_budget_sec`. The proxy tag `FX_EURUSD` is a **hardcoded FE sniper proxy label**, not proof that EUR/USD is armed for entries (EUR/USD remains on `exclude_from_hot_path`).

---

## 4. Multi-Asset Expansion Patch Blueprint

> Documentation / patch stubs only. Apply at a **flat** session via `desk_deploy.sh`. Do **not** change `max_daily_loss_gbp`, REST budget, or instance locks without a separate spec update.

### 4.1 Real epics (from demo throughput + v25 markets)

| Alias | IG epic |
|-------|---------|
| DOW / US30 | `IX.D.DOW.IFM.IP` |
| Nikkei | `IX.D.NIKKEI.IFM.IP` |
| FTSE 100 / UK100 | `IX.D.FTSE.IFM.IP` |
| Gold Spot / GC | `CS.D.CFPGOLD.CFP.IP` |
| EUR/USD | `CS.D.EURUSD.CFD.IP` |

### 4.2 Extend `INSTRUMENT_PNL_SPEC` (GBP floor truth)

```python
# src/trading/open_position_view.py — extend INSTRUMENT_PNL_SPEC
INSTRUMENT_PNL_SPEC: dict[str, dict[str, float | str]] = {
    "IX.D.DOW.IFM.IP": {"point_value": 2.0, "currency": "USD"},
    "IX.D.NIKKEI.IFM.IP": {"point_value": 1.0, "currency": "JPY"},
    "IX.D.SPTRD.IFE.IP": {"point_value": 1.0, "currency": "USD"},
    "CS.D.CFPGOLD.CFP.IP": {"point_value": 1.0, "currency": "USD"},
    "IX.D.DAX.IFM.IP": {"point_value": 1.0, "currency": "EUR"},
    # --- expansion ---
    "IX.D.FTSE.IFM.IP": {"point_value": 1.0, "currency": "GBP"},  # native GBP index
    # EUR/USD: pip_size_for_epic already routes currency=USD; keep explicit:
    "CS.D.EURUSD.CFD.IP": {"point_value": 1.0, "currency": "USD"},
}
```

### 4.3 Per-epic OBI + micro-trail noise filter (config dict)

```python
# Suggested overlay fragment for config_v31_demo_throughput.json
# (merge under existing keys — do not ship until JPY/Gold/FX certification)

"obi_filter_by_epic": {
  "IX.D.DOW.IFM.IP": {"min_abs_ratio": 0.15, "require_align": True},
  "IX.D.FTSE.IFM.IP": {"min_abs_ratio": 0.15, "require_align": True},
  "CS.D.CFPGOLD.CFP.IP": {"min_abs_ratio": 0.18, "require_align": True},  # noisier book
  "CS.D.EURUSD.CFD.IP": {"min_abs_ratio": 0.15, "require_align": True}
},

"feed_quality_by_epic": {
  "IX.D.DOW.IFM.IP": {"max_spread_pts": 12.0},
  "IX.D.FTSE.IFM.IP": {"max_spread_pts": 12.0},
  "CS.D.CFPGOLD.CFP.IP": {"max_spread_pts": 80.0},   # absolute pts ≠ index pts
  "CS.D.EURUSD.CFD.IP": {"max_spread_pts": 3.0}       # pip-style; validate vs product
},

"micro_risk_by_epic": {
  "IX.D.DOW.IFM.IP": {
    "soft_loss_ratio": 0.55,
    "trail_trigger_gbp": 1.0,
    "trail_noise_filter_gbp": 0.15
  },
  "IX.D.FTSE.IFM.IP": {
    "soft_loss_ratio": 0.55,
    "trail_trigger_gbp": 1.0,
    "trail_noise_filter_gbp": 0.20
  },
  "CS.D.CFPGOLD.CFP.IP": {
    "soft_loss_ratio": 0.50,
    "trail_trigger_gbp": 2.5,
    "trail_noise_filter_gbp": 0.40
  },
  "CS.D.EURUSD.CFD.IP": {
    "soft_loss_ratio": 0.50,
    "trail_trigger_gbp": 1.5,
    "trail_noise_filter_gbp": 0.25
  }
},

"dual_core": {
  "exclude_from_hot_path": [
    # Remove only after certification per epic:
    # "IX.D.FTSE.IFM.IP",
    # "CS.D.CFPGOLD.CFP.IP",
    # "CS.D.EURUSD.CFD.IP",
    "IX.D.NIKKEI.IFM.IP"
  ]
}
```

### 4.4 Memory matrix / RuntimeContext expansion stubs

```python
# src/system/runtime_context.py — liquid candidate pool for harnesses
_LIQUID_CANDIDATES: tuple[str, ...] = (
    "IX.D.DOW.IFM.IP",
    "IX.D.FTSE.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "IX.D.NIKKEI.IFM.IP",  # keep last until JPY PnL certified
)

# MemoryContext consumers already key by epic string — no schema change required
# once pnl_gbp / soft_loss_gbp are broker-correct. Optional telemetry tag:
ASSET_MICROSTRUCTURE = {
    "IX.D.FTSE.IFM.IP": {"family": "index_gbp", "obi": 0.15, "atr_tp_mult": 3.5},
    "CS.D.CFPGOLD.CFP.IP": {"family": "metal_usd", "obi": 0.18, "atr_tp_mult": 3.0},
    "CS.D.EURUSD.CFD.IP": {"family": "fx_usd", "obi": 0.15, "atr_tp_mult": 2.5},
}
```

### 4.5 Gate wiring checklist (implementation order)

1. Per-epic `max_spread_pts` in `evaluate_spread_hard_veto` + `evaluate_pre_entry_regime_decision` (stop global 3.0 false positives).
2. Complete `INSTRUMENT_PNL_SPEC` + certify `pnl_gbp_for_open_row` vs IG UPL for each epic.
3. Per-epic micro-trail noise filter in `micro_gbp_exit` (ignore sub-threshold GBP flicker).
4. Remove epic from `exclude_from_hot_path` one-at-a-time; keep `active_stack_slots=2`.
5. Flat deploy → soak → journal WR before next epic.

---

## 5. FE Wiring — Blueprint Viewer

| Artifact | Path |
|----------|------|
| Source markdown | `QUANTUM_ROUTER_AUDIT.md` (repo root) |
| Public copy | `terminal/public/QUANTUM_ROUTER_AUDIT.md` |
| TS mirror | `terminal/src/content/quantumRouterAudit.ts` |
| Modal | `terminal/src/components/QuantumRouterAuditModal.tsx` |
| Trigger | `AIMarketScanner` button label exactly: `📋 EXPOSE MULTI-MARKET DEPLOYMENT BLUEPRINT` |

Open Quantum Terminal → AI Market Scanner panel → click the blueprint button → scrollable high-contrast dark mono viewport (reuses `.blueprint-*` desk styles).

---

## 6. Verification Commands

```bash
# FE typecheck
cd terminal && npx tsc --noEmit

# Focused pytest (desk / memory / quote budget)
IG_AGENT_CONFIG=config/config_v31_demo_throughput.json PYTHONPATH=src \
  python3 -m pytest tests/test_memory_context.py tests/test_entry_quote_budget.py \
  tests/test_desk_self_assess.py -q

# Live flat check (read-only)
curl -s http://127.0.0.1:8080/api/positions/live | python3 -m json.tool | head
```
