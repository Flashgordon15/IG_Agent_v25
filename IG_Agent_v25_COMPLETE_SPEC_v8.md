# IG Agent v25 — Complete Final Specification

**FINAL v8 | June 2026 | CONFIDENTIAL**

> **Live operations:** superseded for day-to-day use by **`IG_Agent_v29.1_COMPLETE_SPEC.md`** (v29.1.0).  
> This v8 document remains the detailed historical reference for the v25.5 chassis.

| Field | Value |
|-------|-------|
| Application version | **25.5.0** |
| Spec version | **v8** (supersedes v1–v7) |
| Foundation | v24-proven (25 May 2026) |
| Date | June 2026 |
| Status | **SHIPPED** — main @ 3ae5aa1 |
| North-star doc | This file + `IG_Agent_v25_COMPLETE_SPEC_v8.pdf` |

---

## Purpose — Read This First

This is the complete, final specification for IG Agent v25 as **implemented and verified** through v25.5.0 (June 2026). It supersedes **all previous spec versions (v1–v7)** and reflects code on `main`, not aspirational design.

**v8 update (June 2026):** Incorporates all development since spec v7 (May 2026):

- Multi-market live operation (Japan 225, Wall Street, Spot Gold, US Tech 100)
- ML confidence blend operational (`USE_ML_SIGNAL=true`)
- Corrected IG contract point values (`ig_point_value_gbp`)
- Lifecycle hardening: graceful shutdown, post-exit verification, manual-stop flag, Safe to Leave
- Dashboard audit (P0–P2): trades dedupe, points CAUTION band, REST /3, changelog splash
- Strategy Help modal (in-dashboard operator reference)
- Startup splash with phase checklist; cold-start bars reduced to 2
- Gap-open block expiry at 12 bars; environment fitness pass ≥55%
- Seven-gate trading loop (documented accurately — v7 “13 gates” mixed pipeline stages with entry gates)
- 500+ pytest suite; cold-start E2E audit script

**Operator quick reference:** Dashboard → **? Strategy help** (mirrors Sections 4–8 below).

---

## 1. What v25 Is — The Step Change

v25 is three step changes in one:

| Step | Change |
|------|--------|
| **1. GUI** | Tkinter → React + FastAPI web dashboard (`localhost:8080`) |
| **2. AI/ML** | Points reinforcement + XGBoost blend + nightly replay pipeline |
| **3. Engine** | v24 operational fixes built in: async orders, position sync, REST budget, deal reconciliation |

### v24 → v25 — Proven vs Shipped

| Dimension | v24 Proven | v25 Shipped (v8) |
|-----------|------------|------------------|
| GUI | Tkinter, beach-ball freezes | React/Vite dashboard, WebSocket ticks |
| Signal gate | Confidence ≥92 only | Rules score + ML blend + fitness + points (effective floor 80% in HEALTHY) |
| Exit management | Fixed stop/target | ATR-scaled breakeven/trail + optional limit extension |
| Learning | None | Points engine + setup memory + ML training store |
| Trade integrity | Phantom trades (fixed) | IG-confirmed closes, BREAKEVEN/PENDING labels, dedupe |
| Session mgmt | Manual restart | Auto refresh, gap protection, session-end flatten |
| Order execution | Sync blocked loop | Async execution engine |
| Position sync | Stopped on watchdog kill | Independent sync thread |
| Watchdog | 60s order timeout | 300s startup grace, manual-stop respect, launchd |
| Markets | Primarily Japan 225 | 4 enabled CFD indices/commodities |
| Monitoring | Tab-based Tkinter | 5-tab web dashboard + Safe to Leave + Strategy Help |

---

## 2. Lessons Learned from v24 — Built Into v25

All v24 production incidents (25 May 2026) have explicit mitigations:

- **REST budget deadlock** → 3 calls/min hard cap, order in-flight 30s timeout
- **Quote staleness** → Lightstreamer hub only; 45s tick max age
- **Duplicate agent processes** → Instance lock + startup orphan cleanup
- **Deal ID mismatch** → Dual ID matching + transaction sync singleton
- **Watchdog kill during order** → Extended grace + async execution
- **Wrong P&L currency** → GBP-normalised learning store; phantom dry-run excluded from daily P&L
- **Spread saturation at open** → 2.5× normal spread cap (not fixed pts only)
- **IG point value wrong** → Per-instrument `ig_point_value_gbp` (Nasdaq 14.94, Dow 7.87, etc.)

---

## 3. Three-Speed Learning System

| Speed | Mechanism | Status |
|-------|-----------|--------|
| **Live** | Every closed trade → points engine + learning DB + ML training hooks | **Operational** |
| **Shadow** | Every `SignalEngine.evaluate()` → `shadow_log.jsonl` | **Operational** |
| **Replay** | Nightly scheduler → historic bars at speed → dataset + optional retrain | **Operational** (optional overnight) |

### 3.1 Nightly Replay Cycle

- Scheduler: `src/system/replay_daily_scheduler.py` / `scripts/` cron examples
- Blocked during live window where configured (22:30–07:00 BST typical)
- Output: replay analysis, training dataset builder, optional XGBoost retrain

### 3.2 Label Weighting

- Live closes weighted highest; replay rows tagged in dataset
- ML training uses **fired signals only** (`fired=true` rows)
- BREAKEVEN labels excluded from binary training target

---

## 4. Points-Based Reinforcement System

**Source:** `src/trading/points_engine.py`  
**Persisted:** `src/data/state/points_state.json`

### 4.1 Scoring Formula

After **5+ confirmed closes** (rolling 20), scoring is confidence-band weighted:

| Result | Band | Points formula |
|--------|------|----------------|
| WIN | high (≥92%) | +3 × (pnl / avg_win) |
| WIN | standard (85–91%) | +2 × (pnl / avg_win) |
| WIN | marginal (80–84%) | +1 flat |
| WIN | low (<80%) | 0 |
| LOSS | high | −4 × (loss / avg_loss) |
| LOSS | standard | −2 × (loss / avg_loss) |
| LOSS | marginal | −1 flat |
| BREAKEVEN | any | 0 |

Before 5 confirmed trades: flat +1 / −1 / 0.

### 4.2 Staged Backing-Off and Recovery

**Nominal state** (from cumulative points):

| Cumulative | Nominal state |
|------------|---------------|
| > +6 | HEALTHY |
| −5 to +6 | CAUTION |
| −30 to −5 | WARNING |
| < −30 | STOP (latched) |

**Effective state** may improve with recovery wins (3 → one notch better; 5 → HEALTHY boost).

| State | Entry confidence bar | Size multiplier (see §8) |
|-------|---------------------|--------------------------|
| HEALTHY | ≥80% (floor rises with bootstrap wins) | Tiered 1×–4× by cumulative + band |
| CAUTION | ≥80% | **0.5×** flat (v8: was split 0.25/0.5 in v7-era code) |
| WARNING | ≥92% only | 0.25× on high band only |
| STOP | No entries | 0× |

**Session guards:**

- **6 consecutive losses** → skip next **1** actionable signal (session pause)
- **>£2000 realised loss in 60 min** → forced WARNING for 30 min
- **Daily GBP halt:** `max_daily_loss_gbp` = **£500** (points_state gate; day-stop via session score disabled)

**Confidence bands:** high ≥92%, standard ≥85%, marginal ≥80%.

---

## 5. Exit Management — ATR Trailing Stops

**Source:** `src/trading/trade_manager.py`

| Mechanism | Implementation (config-driven) |
|-----------|-------------------------------|
| Initial stop | Entry ± adaptive risk points (ATR-based when enabled) |
| Initial target | Entry ± limit (risk × reward multiple, ATR-capped) |
| Breakeven | `breakeven_trigger_points` (30) **or** `breakeven_trigger_atr_multiple` × entry ATR (0.4 for Nasdaq tuning) |
| Trailing | `adaptive_trailing_trigger_points` (50) **or** `trail_trigger_atr_multiple` × ATR (0.75) |
| Trail distance | `adaptive_trailing_distance_points` (25) or ATR band from entry meta |
| Limit extension | Optional (`limit_extension_enabled`): push target by ATR steps, max 3 extensions |
| Max age | `max_position_age_minutes` = 480 (8 h) |
| Session flatten | `auto_flatten_on_session_end` — close all T−N min before session end |
| Broker stops | IG `PUT /positions/otc/{deal_id}` when `broker_stop_management` enabled |

**Iron rule:** Trail and breakeven only move stop in the profit direction.

**Partial close:** Config stub present (`partial_close_enabled: false`) — not yet implemented in v25.5.0.

---

## 6. Session Management

**Source:** `src/trading/session_manager.py`, `src/signals/indicators.py`

| Requirement | Implementation |
|-------------|----------------|
| Auto session refresh | Closed→open transition starts fresh session state |
| Cold start block | **2 bars** (`COLD_START_BARS=2`) — reduced from v7’s documented 6; OHLC pre-seed warms indicators |
| Gap protection | Gap >1× ATR blocked until **12 bars** (`GAP_CLEAR_BARS`) or 60 min wall-clock |
| Session whitelist | Per-instrument `trading_session_whitelist` enforced in `session_open` gate |
| BST session names | `asia_early` 00–07, `london_morning` 07–12, `london_us_overlap` 12–16, `us_afternoon` 16–22, `late` 22–00 |
| Japan 225 maintenance | Hub maintenance flag + `japan225_strategy_paused` |
| Entry near close | Blocked when session ends within configured minutes |
| REST at open | Staggered OHLC bootstrap (22s between REST fetches) |

---

## 7. Environment Fitness System

**Source:** `src/trading/environment_scorer.py`  
**Gate threshold:** `GATE_PASS_MIN = 55` (v7 documented 40 — **updated in v8**)

Four factors (ATR, trend, session, spread) plus client sentiment adjustment. Cold-start and gap-open may cap displayed score. Sentiment crowded long/short applies fitness penalty (Block E).

Dashboard header shows **Fit** score; LIVE tab shows factor breakdown.

---

## 8. Entry Gates — Seven-Gate Decision Flow

**Source:** `src/api/snapshot.py` → `GATE_NAMES`; evaluated in `src/trading/trading_loop.py`

v7 listed 13 “gates” mixing entry checks with post-entry monitoring. **v8 documents the actual seven pre-trade gates** displayed on the dashboard LIVE tab:

| # | Gate | Pass condition | Fail action |
|---|------|----------------|-------------|
| 1 | `session_open` | Market open, not maintenance, in session whitelist, not near session end | WAIT — log detail |
| 2 | `cold_start_gap` | Not in cold start (2 bars); no uncleared gap (>1× ATR) | WAIT |
| 3 | `environment_fitness` | Score ≥ 55% | WAIT |
| 4 | `points_state` | Not STOP; not session pause; daily loss < £500 | WAIT |
| 5 | `risk_validation` | Spread ≤ 2.5× normal; position slots; risk ≤ cap (size clipped if possible) | WAIT |
| 6 | `signal_confidence` | BUY/SELL and confidence ≥ effective threshold; includes **ML blend** | WAIT |
| 7 | `execution` | auto_trade, adaptive checks, correlation guard, live arming, no in-flight order | WAIT / submit async |

**Sub-checks inside gates (not separate dashboard gates):**

- Market suspension (`risk_validation`)
- Correlation guard: max **15** new entries per direction per calendar day (`execution`)
- Adaptive engine blocks: bad setup, low confidence, wide spread (`execution`)
- Circuit breaker: 5 losses → 60 min pause, half size on resume

### 8.1 Signal Confidence & ML Blend

**Rules engine** (`src/signals/signal_engine.py`): EMA 9/21, RSI 14, ATR 14 on **closed** 5m bar; 15m trend filter.

Default global `signal_threshold` / `confidence_floor` = **80**. Per-instrument overrides (e.g. Japan 225: 70).

**ML blend** (inside gate 6, not separate gate):

- `USE_ML_SIGNAL=true`; XGBoost model in `src/data/ml_model/`
- Features: `adjusted_score`, `raw_score`, `rsi`, `atr_ratio` (ATR ÷ stop distance)
- Blend when |prob − 0.5| ≥ **0.15**: `conf = rules×0.6 + prob×100×0.4`, capped 0–100
- Near-50% prob → rules only (no veto)
- Rolling last-20 decisions in dashboard INTELLIGENCE / LIVE ML log

### 8.2 Risk Validation & Position Sizing

**Base size** from instrument `trade_size`, multiplied by `points_engine.get_size_multiplier()`.

| Market | Base size | Stop (pts) | Risk cap | £/pt | Min conf. |
|--------|-----------|------------|----------|------|-----------|
| Japan 225 | 0.4 | 45 | £100 | 5.13 | 70% |
| Wall Street | 0.3 | 80 | £150 | 7.87 | 70% |
| Spot Gold | 6.0 | 10 | £200 | 0.79 | 80% |
| US Tech 100 | 0.25 | 100 | £150 | 14.94 | 75% |

**Clamps:** `adaptive_min_trade_size` 0.01 – `adaptive_max_trade_size` 50; IG min deal size; clip to `risk_cap_gbp`.

**Position limits:** `max_open_positions` = 15 total; `max_positions_per_epic` = 2 base.  
**Dynamic laddering** (HEALTHY only): all epic positions profitable → +1 slot; oldest ≥20 min → +2 slots max.

**Cooldown:** 180 s between entries on same epic.

---

## 9. Reliability Controls (Blocks A–F) — Status

| Block | Area | v8 status |
|-------|------|-----------|
| A | REST budget, margin preflight, suspension, spread median | **Shipped** |
| B | Replay pipeline, scheduler | **Shipped** |
| C | ML hooks, autopsy, dataset, shadow log, scorer | **Shipped** (ML on) |
| D | Intelligence tab APIs | **Shipped** |
| E | Client sentiment | **Shipped** |
| F | Session/points persistence | **Shipped** |

---

## 10. Historical Data Ingestion

- Cache: `src/data/ohlc_cache/{market}_5m.jsonl`
- Status: `src/data/state/ohlc_pull_status.json`
- Yahoo seeder fallback for missing cache (incl. Nasdaq)
- Parallel warm load + staggered REST fetch at startup

---

## 11. ML Pipeline

| Component | Path | Status |
|-----------|------|--------|
| Training store | `ml_training_store.jsonl` | Active |
| Shadow log | `shadow_log.jsonl` | Active |
| Dataset builder | `scripts/` / training pipeline | Active |
| Model | `src/data/ml_model/model.pkl` | Loaded when present |
| Scorer | `src/trading/ml_scorer.py` | Blend at gate 6 |
| Retrain | Replay scheduler / manual | Optional |

---

## 12. Web Dashboard

### 12.1 Technology Stack

| Layer | Technology |
|-------|------------|
| Backend | FastAPI on port 8080 |
| Real-time | WebSocket `/ws` + 5s REST poll fallback |
| Frontend | React 18 + Vite + Tailwind |
| Build | `dashboard/dist/` served by agent |

### 12.2 Five Panels

| Tab | Contents (v25.5.0) |
|-----|---------------------|
| **LIVE** | Gates (7), signal confidence, ML log, Why No Trade, market pills |
| **TRADES** | Active positions, close buttons, closed trades (deduped), BREAKEVEN/PENDING |
| **POINTS** | Cumulative/session/trade scores, state bands, 0.5× CAUTION tooltip |
| **INTELLIGENCE** | Shadow stats, learning progress, replay summary |
| **SYSTEM** | REST /3, uptime, position sync, stop/restart, emergency controls |

### 12.3 Header Barometers

Bid/Offer, stream status, Today P&L, Win (last 20), Fit, Pos count, points pills, sentiment badge, **Strategy help**, Safe to Leave, Stop Agent.

### 12.4 Splash & Lifecycle UX (v8 new)

| Feature | Behaviour |
|---------|-----------|
| Startup splash | Phase checklist on cold start (`StartupSplash.jsx`) |
| Changelog splash | Once per version (`localStorage` + `version.json`) |
| Stop Agent | `POST /api/shutdown` → cleanup → post-exit verifier on :8081 |
| Manual stop flag | `src/data/state/manual_stop.json` — watchdog won't auto-restart |
| Safe to Leave | 13 overnight trust checks (`scripts/safe_to_leave.py`) |
| Strategy Help | In-dashboard operator guide (`strategyHelp.js`) |

### 12.5 Key API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /state`, `WS /ws` | Dashboard snapshot |
| `GET /api/startup/status` | Startup phases |
| `GET /api/splash` | Version changelog |
| `POST /api/shutdown` | Graceful stop |
| `GET /api/shutdown/verify-status` | Post-stop verification |
| `POST /api/safe-to-leave` | Overnight checklist |
| `GET /api/replay/summary` | Replay results |
| `GET /api/shadow/today` | Shadow signal stats |
| `GET /api/learning/status` | ML / DB progress |

---

## 13. Client Sentiment (Block E)

On session open: IG client sentiment cached per epic.  
`crowded_long` → BUY fitness penalty; `crowded_short` → SELL penalty.  
Dashboard header badge when active.

---

## 14. Session & Points Persistence (Block F)

Atomic JSON writes via `state_manager`. Points, correlation guard, manual stop, shutdown verify snapshot, rate-limit state — all survive restart.

---

## 15. Complete System Architecture

### 15.1 Core Components

| Component | Responsibility |
|-----------|----------------|
| `main.py` | FastAPI + agent bootstrap |
| `MarketOrchestrator` | One thread per epic |
| `TradingLoop` | 5s tick, seven gates, snapshot publish |
| `SignalEngine` | Rule-based scoring |
| `MLScorer` | XGBoost probability |
| `PointsEngine` | Cumulative scoring + sizing |
| `EnvironmentScorer` | Fitness factors |
| `AdaptiveEngine` | ATR stops, setup multipliers |
| `ExecutionEngine` | Async IG orders |
| `TradeManager` | Breakeven, trail, exits |
| `IgPositionSync` | REST position reconciliation |
| `Lightstreamer hub` | Live quotes |
| `RestApiBudget` | 3 calls/min |
| `Watchdog` | Auto-restart unless manual stop |

### 15.2 Post-Entry Monitoring (not entry gates)

| Stage | Responsibility |
|-------|----------------|
| Trail monitoring | `TradeManager.update_from_quote` each tick |
| Position sync | 20–60s interval |
| Session flatten | T−N min before close |
| Health monitor | Trading loop heartbeat |
| Drawdown | Daily £500 cap via points gate |

---

## 16. Data Files & Operator Artefacts

| Path | Purpose |
|------|---------|
| `config/config_v25.json` | Primary configuration |
| `src/data/learning_db.sqlite3` | Trades, learning stats |
| `src/data/state/points_state.json` | Points cumulative state |
| `src/data/state/dashboard_snapshot.json` | Last published snapshot |
| `src/data/state/manual_stop.json` | Deliberate stop flag |
| `src/data/.ig_agent_v25.lock` | Single-instance lock |
| `src/data/logs/engine.log` | Primary log |
| `src/data/logs/rate_limit_state.json` | REST backoff |
| `scripts/confirm_stopped.py` | Verify clean shutdown |
| `scripts/live_e2e_audit.py` | Cold-start API audit |
| `scripts/pre_flight_check.py` | Pre-live checklist |

---

## 17. Configuration

**Loader:** `config_v25.json` → `config_v24.json` → legacy fallback.

**Critical globals (v25.5.0):**

| Key | Value | Notes |
|-----|-------|-------|
| `signal_threshold` | 80 | Pre-filter + gate floor |
| `confidence_floor` | 80 | Bootstrap rises +1/win |
| `USE_ML_SIGNAL` | true | Blend at gate 6 |
| `max_daily_loss_gbp` | 500 | Hard daily halt |
| `max_open_positions` | 15 | Portfolio cap |
| `max_positions_per_epic` | 2 | + dynamic laddering |
| `cooldown_seconds` | 180 | Per-epic entry spacing |
| `rsi_buy_max` | 85 | Hard RSI block |
| `rsi_sell_min` | 15 | Hard RSI block |
| `auto_flatten_on_session_end` | true | Session close flatten |

Per-instrument overrides in `instruments.{key}.*`.

---

## 18. Operations

### 18.1 Launch

- Desktop icon → `launcher/IG Agent v25.app` → `launch.sh`
- Fast path (~1s): agent already healthy on :8080 → open browser only
- Cold path (~30–45s): full startup + splash

### 18.2 Stop

1. Dashboard **Stop Agent** or `POST /api/shutdown`
2. Sets manual-stop flag; spawns `shutdown_verify_server.py`
3. Verify: `PYTHONPATH=src python3 scripts/confirm_stopped.py`

### 18.3 Watchdog & launchd

- `scripts/watchdog.sh` — 30s check; respects `manual_stop_active()`
- `scripts/install_launchd.sh` — agent + caffeinate + watchdog plists
- Stale lock after crash: `rm -f src/data/.ig_agent_v25.lock`

### 18.4 Pre-Live Checklist

```bash
PYTHONPATH=src python3 scripts/pre_flight_check.py --live
PYTHONPATH=src python3 scripts/e2e_platform_validation.py
POST /api/safe-to-leave  # expect 13/13
```

### 18.5 Tests

```bash
PYTHONPATH=src python3 -m pytest tests/ -x -q
```

---

## 19. Multi-Instrument Architecture

### 19.1 Enabled Instruments (v25.5.0)

| Key | Epic | Priority |
|-----|------|----------|
| `japan_225` | IX.D.NIKKEI.IFM.IP | default |
| `wall_street` | IX.D.DOW.IFM.IP | 70 |
| `gold` | CS.D.CFPGOLD.CFP.IP | 100 |
| `nasdaq_100` | IX.D.NASDAQ.IFM.IP | 65 |

Disabled in config but scaffolded: EUR/USD, GBP/USD, US Oil, Germany 40.

### 19.2 Activation

Set `enabled: true` in instrument block; restart agent; OHLC bootstrap + orchestrator thread starts automatically.

---

## 20. Delivery Status — Eight-Week Plan

| Phase | Target | v8 status |
|-------|--------|-----------|
| Core engine + v24 fixes | Week 1–2 | **Done** |
| Web dashboard | Week 3–4 | **Done** (5 tabs) |
| Points + learning | Week 4–5 | **Done** |
| ML pipeline | Week 5–6 | **Done** (blend on) |
| Multi-market + hardening | Week 6–8 | **Done** (v25.5.0) |
| Live gate (16 checks) | Pre-live | **Scripts ready** — run Safe to Leave |

---

## 21. Live Trading Gate — Operator Checklist

Before unattended operation:

1. Agent HEALTHY on `/api/health`
2. Safe to Leave 13/13
3. Watchdog active OR deliberate manual-stop understood
4. `max_daily_loss_gbp` appropriate for account
5. Demo/LIVE mode verified in config
6. Dashboard Strategy Help reviewed for current thresholds

---

## 22. Component Safety and Performance

- **Safe defaults:** Gate/scorer exceptions → WAIT, not trade
- **ML timeout:** 0.5s subprocess cap; falls back to rules
- **Quote freshness:** 45s max tick age
- **REST:** 3/min atomic budget
- **Performance:** One thread per market; snapshot publish per tick

---

## 23. Operational Safety

| Control | Implementation |
|---------|----------------|
| Emergency stop | Dashboard + `scripts/emergency_stop.sh` |
| Flatten all | `POST /api/flatten/all` |
| Multi-instance | Instance lock + orphan kill on startup |
| Manual stop | Prevents watchdog restart until icon launch |
| Drawdown | £500 daily + points STOP latch |

---

## 24. Absolute Rules — Non-Negotiable

1. **Never** trade without IG-confirmed position sync on live
2. **Never** run two agents (lock file)
3. **Never** commit credentials to disk
4. **Never** bypass points STOP without explicit operator reset
5. **Never** edit `points_state.json` or SQLite WAL while agent running
6. Dashboard is **read-only** for trading state (controls except authorised POST endpoints)
7. Rebuild `dashboard/dist` after any `dashboard/src` change

---

## 25. Current Codebase State (June 2026)

| Item | Status |
|------|--------|
| Branch | `main` @ 3ae5aa1 |
| App version | 25.5.0 |
| Spec | v8 (this document) |
| PR | #4 merged — lifecycle + dashboard audit |
| Tests | 500+ pytest passing |
| E2E audit | `live_e2e_audit.py` — 19 API checks |
| Markets live | Japan 225, Wall St, Gold, Nasdaq |
| ML | Trained model blend active |
| Tkinter | Removed |
| Dashboard | React dist served on :8080 |

---

## 26. v7 → v8 Change Log (Spec Corrections)

| Topic | v7 doc | v8 actual |
|-------|--------|-----------|
| Gate count | 13 entry+monitor gates | **7** pre-trade gates on LIVE tab |
| Fitness pass | ≥40% | **≥55%** |
| Production confidence | 92% default | **80%** floor (WARNING 92%) |
| ML gate | Separate gate 9 | **Blend inside gate 6** |
| CAUTION size | 0.75× | **0.5×** flat ≥80% |
| HEALTHY threshold | >+10 pts | **>+6 pts** |
| STOP threshold | <−30 (same) | Same; latched |
| Cold start | 6 bars | **2 bars** |
| Gap clear | vague | **12 bars** / 60 min |
| Spread cap | 1.5× implied | **2.5×** normal |
| £/pt | 1.0 implied | **Per-instrument** (Nasdaq 14.94) |
| Session loss pause | 3 skips after 3 losses | **1 skip after 6 losses** |
| Day stop | session score <−5 | **Disabled** — use £500 daily |
| Partial close | Specified | **Config stub only** |
| Dashboard tabs | 4 + intel | **5 tabs** + Strategy Help |
| Shutdown | Basic | **Verify server + manual stop** |

---

*IG Agent v25 — Complete Final Specification v8 — Confidential*  
*Web Dashboard | Points Reinforcement | ML Blend | Multi-Market | Lifecycle Hardening | June 2026*
