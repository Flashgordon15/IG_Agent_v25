# IG Agent v25 — Change Log

Every code change made by the AI agent is recorded here, oldest first.
Each entry states the file changed, what was wrong, and what was fixed.
The corresponding regression test lives in `tests/test_deployed_fixes.py`.

---

## Session 1 — 2026-06-05 (pre-summary)

### Trading enhancements
- **`config/config_v25.json`** — Added `london_morning` to Wall Street and Nasdaq `trading_session_whitelist`.
- **`src/trading/session_manager.py`** — Added `GAP_CLEAR_BARS = 12` constant (1 hour = 12 × 5-min bars).
- **`src/trading/trading_loop.py`** — `_gate_cold_start_gap`: gap block now expires after `GAP_CLEAR_BARS` bars.
- **`src/signals/signal_engine.py`** — Clamped `raw_conf` and `adjusted` to max 100 before returning `SignalResult`.
- **`src/trading/session_summary.py`** — Sanitised mock objects in `write_session_end_summary` (test-env safety).
- **ML rebuild** — Replayed historical signals, built training dataset, retrained XGBoost model.

### Startup splash screen
- **`src/system/startup_tracker.py`** (new) — Thread-safe singleton tracking 8 startup phases with progress %.
- **`src/main.py`** — Instrumented with `_startup_mark()` calls at each boot phase.
- **`src/runtime/agent_bootstrap.py`** — `_startup_mark()` calls for database, OHLC, and trading-loop phases.
- **`src/api/routes.py`** — Added `GET /api/startup/status` endpoint.
- **`dashboard/src/components/StartupSplash.jsx`** (new) — Animated startup splash with phase checklist and OK button.
- **`dashboard/src/App.jsx`** — Always shows `StartupSplash` on fresh load; transitions to dashboard on OK.

### P&L fix
- **`src/data/learning_store.py`** — `sum_daily_pnl` excludes dry-run trades with `ig_pnl_currency=0` to prevent phantom P&L corruption.

---

## Session 2 — 2026-06-05

### Bug: gap expiry never fired (critical — all markets blocked)
- **`src/trading/session_manager.py`** — Added `elapsed_bars_since_open()` (uncapped). The existing `bars_since_open()` is hard-capped at `COLD_START_BARS=6`; `GAP_CLEAR_BARS=12` was therefore unreachable, meaning the gap block never expired.
- **`src/trading/trading_loop.py`** — `_gate_cold_start_gap` now uses `elapsed_bars_since_open()` for the expiry check and `bars_since_open()` only for the cold-start display counter.

### Bug: RSI buy cap too restrictive
- **`config/config_v25.json`** — `rsi_buy_max` raised from 78 → 80 (standard overbought line). At 78, Nikkei RSI 79.9 with 96% confidence was permanently filtered.

### Feature: ML decision log wired to dashboard
- **`src/trading/trading_loop.py`** — Added `_ml_decision_log` (rolling 20-entry list per market). Populated every time ML blending runs in `_gate_signal_confidence`. Included in snapshot payload as `ml_decision_log`.
- **`dashboard/src/components/LivePanel.jsx`** — Updated `fmtLogLine` to render ML blend entries with market, direction, ML prob, rules conf, blended conf.

### GUI fixes — disconnected fields
- **`src/api/dashboard_data.py`** — `get_system_info()` now returns `caffeinate_pid`, `caffeinate_running`, `ohlc_markets_cached`, `uptime_s`, `sessions_passed`, `sessions_required`. These were read by `SystemTab` but never returned.
- **`src/api/snapshot_store.py`** — `_tick_for_readers()` now injects `ohlc_markets_cached`, `model_version`, `last_retrain_time`, `uptime`, `position_sync_status` into every snapshot so `SystemPanel` shows real data instead of "—".
- **`src/api/intelligence_data.py`** — `shadow_today()`: fixed `top_blocked_setup` (was always "unknown" due to empty-key rows); added `top_3_setups` array; fixed `estimated_extra_if_threshold_minus_5` (was counting all 5492 blocked rows; now counts only those within 5 pts of threshold → 459).
- **`dashboard/src/components/Header.jsx`** — Sentiment crowd badge was permanently invisible: compared dict `{label, value}` to string `"crowded_long"`. Fixed to read `sentiment?.label`.
- **`src/trading/trading_loop.py`** — `rest_calls_min` was hardcoded to `0` in the snapshot. Replaced with `_rest_calls_last_minute()` backed by `RestApiBudget.calls_last_minute()`.

---

## How to verify before each restart

Run: `PYTHONPATH=src python3 -m pytest tests/test_deployed_fixes.py -v`

All tests must pass. Each test maps directly to one row in this changelog.
