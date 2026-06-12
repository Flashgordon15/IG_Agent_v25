# IG Agent v29.1 — Complete Specification

**FINAL v29.1 | June 2026 | CONFIDENTIAL**

| Field | Value |
|-------|-------|
| Application version | **29.1.0** (`APP_VERSION` in `src/system/app_identity.py`) |
| Spec version | **v29.1** (supersedes v29.0 overlay notes and operational v25 spec v8 as *live* reference) |
| Config overlay | `config/config_v29.json` → extends `config_v25.json` |
| Foundation | v25 chassis + v26 profitability overlay + v29 execution/scalping hardening |
| Status | **SHIPPED** — `main` @ `a96aad0`+ |
| Architecture doc | `docs/V29.1_ARCHITECTURE.md` |
| Legacy spec | `IG_Agent_v25_COMPLETE_SPEC_v8.md` (historical v25.5 detail) |
| Strategic north-star | `IG_Agent_v26_FRAMEWORK.md` (future multi-strategy brain) |

---

## Purpose — Read This First

This document is the **authoritative specification for IG Agent v29.1** as implemented on `main`. It describes what the agent **does today** in DEMO deployment: multi-market CFD trading on IG, React dashboard, protective learning phase, live P&L streaming, and Profile B learning-demo integrity.

**v29.1 is not a greenfield rewrite.** It is the consolidated production agent (v25 execution chassis + v29 overlays) with June 2026 hardening for:

- Live dashboard P&L accuracy (indices + FX pip math)
- Protective learning (clean labels, raised entry bar, setup registry)
- Daily loss baseline reset and soft/hard pause semantics
- ML / shadow / stats pipeline repairs (silent failures eliminated)
- Learning Health observability (API + dashboard + CLI)

**Operator quick reference:** Dashboard → **System** (Learning Health, REST budget, stop controls) · **? Strategy help** · `scripts/learning_health_report.py`

---

## 1. Lineage — v25 → v29.1

| Generation | Role | Status |
|------------|------|--------|
| **v25** | Web dashboard, seven gates, points engine, ML blend, position sync, REST budget | **Chassis — kept** |
| **v26 overlay** | Profitability config (`config_v26.json`), capital envelope, expectancy, certification hooks | **Merged at load** |
| **v29** | Scalping framework, execution protect, correlation guard, demo-only guard, chaos E2E | **Shipped** |
| **v29.1** | Protective learning, P&L drift fixes, learning health, daily loss baseline reset, Profile B tightening | **Current** |

Config resolution order (see `ConfigLoader`):

```
config_v29.json  →  config_v25.json  →  config_v24.json  →  legacy fallback
```

Runtime identity: `APP_VERSION_LABEL = "v29.1"`, instance lock `.ig_agent_v29.lock`.

---

## 2. Operating Mode (v29.1)

| Setting | Value | Source |
|---------|-------|--------|
| `operating_mode` | **DEMO** | `config_v29.json` |
| `demo_only_deployment` | **true** | Blocks live order routing |
| `allow_live_trading` | **false** | Hard guard |
| Profile | **B** (`learning_demo_mode.profile`) | Labelled throughput with integrity gates |
| Daily soft pause | **£400** realised | `learning_demo_mode.daily_loss_soft_pause_gbp` |
| Daily hard stop | **£2,000** effective | `capital_envelope.max_daily_loss_gbp` (v26 overlay) |
| Protective conf floor | **62%** | `protective_learning.signal_threshold_floor` |
| Protective fitness floor | **55** | `protective_learning.fitness_min_floor` |

### 2.1 v29.1 Daily Loss Baseline

On startup, `v291_upgrade.py` archives today's raw SQLite P&L as a **baseline** so **effective daily loss starts at £0** after restart/upgrade. Audit rows are preserved; operators can resume demo entries without carrying forward a stale loss figure.

- Runtime key: `daily_loss_baseline_v291` in learning store
- Policy: `src/system/daily_loss_policy.py` → `effective_daily_pnl()`
- Manual script: `scripts/apply_v291_daily_loss_reset.py`

---

## 3. Enabled Markets (v29.1)

Instruments are defined in `config_v25.json` with v29 overrides in `config_v29.json`.

| Market | Epic | Sessions (typical) | Notes |
|--------|------|-------------------|-------|
| Japan 225 | `IX.D.NIKKEI.IFM.IP` | `asia_early` | Primary index |
| Wall Street | `IX.D.DOW.IFM.IP` | overlap, US afternoon | USD P&L spec |
| US Tech 100 | `IX.D.NASDAQ.IFM.IP` | overlap, US afternoon | High £/pt |
| Spot Gold | `CS.D.CFPGOLD.CFP.IP` | London + US | USD P&L spec |
| EUR/USD | `CS.D.EURUSD.CFD.IP` | London + overlap | **Pip-scaled P&L** |
| GBP/USD | `CS.D.GBPUSD.CFD.IP` | London + overlap | Pip-scaled |
| US Oil WTI | `CS.D.CRUDE.CFD.IP` | **disabled** | Re-enable after OHLC + stream seed |

Per-instrument `ig_point_value_gbp`, `trade_size`, `stop_distance_points`, and session whitelists remain in `config_v25.json`.

---

## 4. Entry Gates — Seven-Gate Flow (+ v29 Subsystems)

**Source:** `src/trading/trading_loop.py`, `src/api/snapshot.py` (`GATE_NAMES`)

| # | Gate | Pass (summary) |
|---|------|----------------|
| 1 | `session_open` | Open, whitelisted session, not near close |
| 2 | `cold_start_gap` | Past cold start; gap cleared |
| 3 | `environment_fitness` | Score ≥ effective floor (**55+** in protective phase) |
| 4 | `points_state` | Not STOP; daily loss policy OK |
| 5 | `risk_validation` | Spread, slots, risk cap, size clip |
| 6 | `signal_confidence` | Rules + optional ML blend; **floor 62%** when protective learning on |
| 7 | `execution` | auto_trade, correlation, protect paths, no in-flight deadlock |

### 4.1 Profile B / Demo Soak (v29.1 tightened)

`demo_soak_mode` relaxations for learning throughput — **reduced in v29.1**:

| Flag | v29.0 (loose) | v29.1 |
|------|---------------|-------|
| `bypass_ml_veto` | true | **false** |
| `disable_rotation_filter` | true | **false** |
| `require_points_healthy` | false | **true** |
| `fitness_min` | 50 | **55** |
| `spread_to_atr_circuit_max` | 0.4 | **0.35** |

### 4.2 Protective Learning

**Module:** `src/system/protective_learning.py`

When `protective_learning.enabled: true`:

- Raises signal threshold floor to **62%** (via `signal_engine._effective_signal_threshold`)
- Raises fitness floor to **55** (via `gate_relaxation.effective_fitness_min`)
- Raises points confidence floor (via `points_engine.trade_confidence_threshold`)

Purpose: collect **agent-sourced** closes with higher entry quality while ML/registry mature.

### 4.3 ML Blend & Filter Overrides

- Blend requires trained model + **≥500** training records (`trading_loop` ML gate)
- Near-50% model probability → rules only (no veto)
- **`meta.json` filter overrides** (e.g. `max_rsi`) applied at signal time via `ml_filter_overrides.py` even before blend is active

### 4.4 Execution Protect & Scalping Framework

**Modules:** `src/execution/scalping/`, `config_v29.json` → `execution_protect`, `scalping_framework`

- Spread MA gate at execution boundary
- Atomic SL/TP payload verification
- Micro breakeven buffer; optional limit-at-touch
- Daily equity drawdown circuit (scalping block)

Does **not** alter signal indicators — execution boundary only.

---

## 5. P&L & Dashboard Accuracy (v29.1)

### 5.1 Open Position P&L

**Module:** `src/trading/open_position_view.py`

| Instrument class | Behaviour |
|------------------|-----------|
| **Indices** | Point move × size × `ig_point_value_gbp` |
| **FX CFDs** | Price delta → **IG pip points** (`÷ 0.0001` majors) × size × £/pip |
| **USD epics** | UPL converted via live GBP/USD hub quote |

**Quote trust guard:** If streaming mark is on a wildly different price scale than entry (e.g. stale wrong epic), **IG broker UPL is preserved** — prevents dashboard drift (e.g. −£64,900 on Nikkei).

**Hub refresh:** `snapshot_store.push_hub_quote_to_dashboard()` merges Lightstreamer quotes and recomputes open P&L; bypasses 250ms throttle when positions are open.

### 5.2 Daily P&L Display

```
daily_pnl_gbp = realized_daily_pnl_gbp + open_unrealized_gbp
```

- Trading loop publishes `realized_daily_pnl_gbp` from `effective_daily_pnl(store)`
- `apply_display_daily_pnl()` is **idempotent** on WebSocket re-read (no double-count)

### 5.3 FX Display Precision

- Prices: 5 dp (`dashboard/src/utils/fmtPrice.js`)
- Pip points: 2 dp (`dashboard/src/utils/fmtPts.js`)

---

## 6. Learning System (v29.1)

### 6.1 Three Speeds (unchanged architecture)

| Speed | Mechanism | v29.1 status |
|-------|-----------|--------------|
| **Live** | Closed trades → points + SQLite + ML store | **Fixed** — imports repaired |
| **Shadow** | Every `evaluate()` → `shadow_log.jsonl` | **Fixed** — all return paths log |
| **Replay** | Nightly scheduler / manual | Operational |

### 6.2 Clean Learning Inputs

**Module:** `src/system/learning_trade_policy.py`

Excluded from setup stats, ML training, and registry rebuild:

- Setup keys matching `IG|…` or `IMPORT` (anchored regex)
- Sources: `ig_import`, IG transaction history rows
- `dry_run` trades (except shadow counterfactuals — see below)

**Impact:** ~71 IG-import closes no longer pollute win-rate or ML labels.

### 6.3 Setup Registry

**Modules:** `src/system/setup_registry_refresh.py`, `src/data/state/setup_registry.json`

- Rebuilt at startup from **agent-only** closes
- Ban threshold: **≥5 trades** and win rate below ban WR
- Gate: `setup_registry.enabled` — bans applied in signal path when active

CLI: `python3 scripts/refresh_setup_registry.py`

### 6.4 Shadow → Stats Pipeline

Shadow counterfactual rows (`source='shadow'`, `dry_run=1`) **are included** in `_rebuild_stats_for()` so replay/shadow learning feeds setup stats without polluting live P&L.

### 6.5 Learning Health

**Modules:** `src/system/learning_health.py`, `scripts/learning_health_report.py`

**API:** `GET /api/learning-health`

Dashboard: **System → Learning Health** panel

Reports: agent vs IG-import trade counts, ML readiness (N/500), registry bans, protective mode, sentiment guard status.

---

## 7. Sentiment Guard (v29.1)

**Config:** `sentiment_guard` in `config_v29.json`

- IG `/clientsentiment` via `EnvironmentScorer`
- Crowded bands: **70% / 30%** (was 80/20)
- **−10 fitness** when crowded; no direction veto
- Raw sentiment logged when `log_raw: true` → check `engine.log`

---

## 8. Risk & Stop Maths (v29.1)

**Module:** `src/execution/trade_risk.py` + `src/system/pnl_math.py`

| Class | Stop distance | Risk £ |
|-------|---------------|--------|
| Index | IG points = price points | `pts × size × ig_point_value_gbp` |
| FX | IG points = **pips** (1 pt = 0.0001 on majors) | `pips × size × ig_point_value_gbp` |

`resolve_stop_price()` converts configured `stop_distance_points` to price delta per epic.

---

## 9. Web Dashboard (v29.1)

### 9.1 Stack

| Layer | Technology |
|-------|------------|
| Backend | FastAPI `:8080` (`src/main.py`) |
| Real-time | WebSocket `/ws` + snapshot file |
| Frontend | React 18 + Vite + Tailwind |
| Build | `dashboard/dist/` (rebuild after `dashboard/src/` edits) |

### 9.2 Tabs & Key Panels

| Tab | v29.1 additions |
|-----|-----------------|
| **LIVE** | Live P&L from streaming quotes; FX pip display |
| **TRADES** | Flatten/close; deduped closed trades |
| **SYSTEM** | **Learning Health**, ML metadata, REST /3, stop/restart |
| **Header** | Today P&L (realized + open), Fit, points, digest/roadmap |

### 9.3 API Endpoints (selected)

| Endpoint | Purpose |
|----------|---------|
| `GET /state`, `WS /ws` | Dashboard tick |
| `GET /api/learning-health` | ML/registry/agent P&L health |
| `GET /api/daily-digest` | Morning digest markdown |
| `GET /api/roadmap/progress` | Roadmap telemetry |
| `GET /api/learning/status` | ML record counts |
| `POST /api/replay/run` | Manual replay pipeline |
| `POST /api/shutdown` | Graceful stop |

Full list: `src/api/routes.py`

---

## 10. Reliability & Supervision

| Control | Implementation |
|---------|----------------|
| REST budget | 3 calls/min — `RestApiBudget` |
| Instance lock | `.ig_agent_v29.lock` |
| Manual stop | `manual_stop.json` — blocks watchdog |
| Watchdog | launchd `com.igagent.v25.watchdog` |
| Post-stop verify | `:8081` verifier |
| Tests | **794+** pytest (`tests/`) |

---

## 11. Data Files & Operator Artefacts

| Path | Purpose |
|------|---------|
| `config/config_v29.json` | v29.1 overlay (primary operator edits) |
| `config/config_v25.json` | Instrument matrix, thresholds, sizes |
| `src/data/learning_db.sqlite3` | Trades, setup stats |
| `src/data/ml_training_store.jsonl` | ML training log |
| `src/data/shadow_log.jsonl` | Shadow evaluations |
| `src/data/state/setup_registry.json` | BAN/PROBE/ACTIVE setups |
| `src/data/state/dashboard_snapshot.json` | Cross-process tick |
| `src/data/.ig_agent_v29.lock` | Single-instance lock |

---

## 12. Run & Verify

```bash
# Run agent (from repo root)
PYTHONPATH=src python3 src/main.py

# Dashboard
open http://localhost:8080

# Rebuild UI after edits
cd dashboard && npm run build

# Learning health CLI
PYTHONPATH=src python3 scripts/learning_health_report.py

# Full test suite
PYTHONPATH=src python3 -m pytest tests/ -q
```

**After code or config changes:** restart agent (desktop launcher or manual). Hard-refresh browser if dashboard cached.

---

## 13. Version Control

| Commit | Summary |
|--------|---------|
| `4c324d7` | v29.1 protective learning, live P&L, learning health dashboard |
| `a96aad0` | ML pipeline import fixes, P&L drift, FX risk, shadow logging |

Tag releases against `APP_VERSION` in `src/system/app_identity.py` and changelog in `src/data/version.json`.

---

## 14. Document Map

| Document | Audience |
|----------|----------|
| **This file** | Operators + implementers — **current truth** |
| `docs/V29.1_ARCHITECTURE.md` | Engineers — modules, data flow, diagrams |
| `IG_Agent_v25_COMPLETE_SPEC_v8.md` | Historical v25.5 detail |
| `IG_Agent_v26_FRAMEWORK.md` | Future multi-strategy / £50k vision |
| `CLAUDE.md` | AI coding assistant entry point |

---

*End of IG Agent v29.1 Complete Specification*
