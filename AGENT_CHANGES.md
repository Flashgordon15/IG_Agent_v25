# IG Agent v25 — Change Log

Every code change made by the AI agent is recorded here, oldest first.
Each entry states the file changed, what was wrong, and what was fixed.
The corresponding regression test lives in `tests/test_deployed_fixes.py`.

---

## Session — 2026-06-07 (profitability improvements)

- **`scripts/profitability_report.py`** — per-epic WR/P&L report; `--reconcile` backfills epic + tags legacy rows
- **`scripts/analyse_replay.py`** — fix 0% WR: accept `label_3` from batch replay
- **`scripts/replay_signals.py`** — write `label_3bar` alias in batch path
- **`config/config_v25.json`** — Japan 85% threshold; US indices drop london_morning; `one_position_per_epic: true`; partial close enabled
- **`correlation_guard.py`** — `MAX_NEW_PER_DIRECTION` 15 → 5
- **`points_engine.py`** — HEALTHY cumulative >4 (was >6)
- **`trading_loop.py`** — ML blend gated on ≥500 training records
- **`signal_engine.py`** — soft ×0.9 penalty in high vol regime
- **`trade_manager.py`** + **`config.py`** — `partial_close_enabled` config guard
- **`tests/test_profitability_improvements.py`** — regression tests
- **`docs/PROFITABILITY_ASSESSMENT_2026-06-07.md`** — assessment + changelog

---

## Session — 2026-06-07 (spec v8)

- **`IG_Agent_v25_COMPLETE_SPEC_v8.md`** — New north-star spec reflecting shipped v25.5.0 (supersedes v7 PDF)
- **`IG_Agent_v25_COMPLETE_SPEC_v8.pdf`** — Generated via `scripts/generate_spec_pdf.py`
- **`CLAUDE.md`** — Architecture reference updated to v8

---

## Session — 2026-06-07 (v25.5.0 merge & shutdown)

**Handoff:** `docs/SESSION_HANDOFF_2026-06-07.md`

- Merged PR #4 → `main` @ `3ae5aa1` (lifecycle hardening, dashboard audit, overnight ops)
- Resolved 9 merge conflicts; kept feature-branch lifecycle/shutdown work; integrated `safe_to_leave.py` from main
- Post-merge: 56 tests pass; agent verified on 25.5.0; Safe to Leave 13/13
- Full backup: `IG_Agent_v25_full_backup_20260607_082507.tar.gz`
- End-of-session: graceful shutdown, watchdog killed, caffeinate launchd unloaded, manual-stop flagged

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

## Session 6 — 2026-06-05 (v25.3.0)

### Feature: Confidence-tiered dynamic position sizing

**Motivation:** Flat sizing treats a 95% confidence trade the same as an 80% confidence trade. Dynamic sizing rewards high-conviction signals with proportionally larger positions while protecting capital on marginal entries.

**Changes:**

- **`config/config_v25.json` — `dynamic_sizing` block added (top level):**
  - `enabled: true` — active immediately.
  - `account_balance_gbp: 10000` — account reference for future margin % checks.
  - `max_margin_pct: 0.15` — max 15% margin utilisation per trade (reference, not yet enforced in code).
  - Four confidence tiers: ≥95 → 1.0×, ≥90 → 0.65×, ≥85 → 0.4×, ≥80 → 0.25×.

- **`config/config_v25.json` — base `trade_size` updated per instrument** so that `base_size × 1.0 × ig_point_value_gbp × stop_pts ≤ £375` (top-tier risk):

  | Instrument | Old size | New size | Risk at 1.0× tier | Risk at 0.25× tier |
  |---|---|---|---|---|
  | Nasdaq 100 | 0.05 | **0.25** | £373.50 (0.25×£14.94×100) | £93.38 |
  | Wall Street | 0.1 | **0.3** | £188.88 (0.3×£7.87×80) | £47.22 |
  | Spot Gold | 6.0 | **6.0** | £47.40 (unchanged) | £11.85 |
  | Japan 225 | 0.2 | **0.4** | £92.34 (0.4×£5.13×45) | £23.09 |

- **`config/config_v25.json` — `trailing_stop` block:** Added three new keys:
  - `partial_close_enabled: false` — config stub; logic not yet implemented.
  - `partial_close_at_r: 1.5` — trigger at 1.5R profit.
  - `partial_close_fraction: 0.5` — close 50% of position.

- **`src/execution/execution_engine.py` — `_confidence_adjusted_size` method added:**
  - Reads `dynamic_sizing.tiers` from config, sorted highest-first.
  - Returns `base_size × tier["size_multiplier"]` for the highest tier that `confidence ≥ min_confidence`.
  - Falls back to lowest tier multiplier when confidence is below all tiers.
  - Called in `get_execution_settings()` after the points-engine multiplier, so the full chain is: `base_size × points_state_mult × confidence_tier_mult`.
  - Notes appended to execution settings: `conf-tier ×N.NN`.

- **`config/config_v25.json` — version bumped** `25.2.2` → `25.3.0`.

### Nasdaq effective sizes at each tier (base_size=0.25, stop_pts=100):

| Confidence | Tier mult | Effective size (after 0.5× CAUTION) | Risk (£/pt=14.94) |
|---|---|---|---|
| ≥ 95% | 1.0× | 0.125 | £186.75 |
| ≥ 90% | 0.65× | 0.0813 | £121.39 |
| ≥ 85% | 0.4× | 0.05 | £74.70 |
| ≥ 80% | 0.25× | 0.0313 | £46.74 |

*(In HEALTHY state the points multiplier is 1.0× so sizes double)*

### Tests added — `tests/test_deployed_fixes.py`
New class `TestSession6DynamicSizing` with 4 tests:
- `test_dynamic_sizing_config_present` — `dynamic_sizing.enabled=True`, 4 tiers present.
- `test_confidence_tiered_sizing` — conf=95 → 1.0×, conf=82 → 0.25× via `_confidence_adjusted_size`.
- `test_nasdaq_base_size_updated` — Nasdaq `trade_size=0.25`.
- `test_partial_close_keys_present` — `trailing_stop` has all 3 partial-close keys, `enabled=False`.

---

## How to verify before each restart

Run: `PYTHONPATH=src python3 -m pytest tests/test_deployed_fixes.py -v`

All tests must pass. Each test maps directly to one row in this changelog.

---

## Session 3 — 2026-06-05 (v25.3.0)

### Environment scorer cold start cap
- **`src/trading/environment_scorer.py`** — `COLD_START_BAR_CAP` reduced from 6 to 2 bars (aligned with `session_manager.COLD_START_BARS`). Fitness now reaches 100% after ~10 real minutes instead of 30. Backdate log message improved to show the cap value.

### Dashboard blended confidence
- **`src/trading/trading_loop.py`** — `_build_snapshot_payload`: `signal.confidence` now reads the ML-blended value from the `signal_confidence` gate (`g.value["confidence"]`) instead of the raw rules-only `sig.adjusted_confidence`. Added `rules_confidence` (raw rules %) and `threshold_delta` (confidence − floor) to the signal dict for dashboard transparency.

### OHLC bootstrap rate-limit stagger
- **`src/trading/ohlc_bootstrap.py`** — `bootstrap_ohlc_parallel` split into two phases: (1) markets with warm local cache load in parallel (no REST budget consumed); (2) markets needing a REST fetch run sequentially with a 22-second stagger between calls. Added `_OHLC_REST_STAGGER_SEC = 22.0` constant. Added `import time`.

### Nasdaq OHLC cache
- **`src/data/ohlc_yahoo_seeder.py`** — Added `"IX.D.NASDAQ.IFM.IP": ("NQ=F", "US Tech 100")` to `EPIC_YAHOO_MAP` and `DEFAULT_SEED_EPICS`. Nasdaq OHLC history can now be fetched from Yahoo Finance at startup.

### Startup OHLC pre-seed
- **`src/runtime/agent_bootstrap.py`** — Before `bootstrap_ohlc_parallel`, iterates enabled markets and calls `fetch_yahoo_ohlc_for_epic()` for any with a missing or empty cache file. Markets already cached are skipped.

### Startup self-test
- **`src/system/startup_tracker.py`** — Added `self_test` phase (at 55%) between `database` and `ohlc`.
- **`src/runtime/agent_bootstrap.py`** — After `_startup_mark("database")`, runs `tests/test_deployed_fixes.py` via subprocess with a 60-second timeout. Marks `self_test` phase done (or skipped on error).

### Pre-startup process cleanup
- **`src/main.py`** — Added `_pre_startup_cleanup()`: kills any stale `src/main.py` processes via `SIGTERM` and removes the stale instance lock file. Called at the top of `main()` before `AgentRuntime`.

### Tests updated
- **`tests/test_session_manager.py`** — `test_cold_start_under_cap_bars` and `test_cold_start_advances_with_elapsed_time` rewritten to use `COLD_START_BARS` constant; `test_state_persistence_round_trip` bars_elapsed assertion uses constant. Removed hardcoded 6.
- **`tests/test_trade_eligibility.py`** — `test_build_cold_start_from_gates` display assertion uses `COLD_START_BARS` constant.
- **`tests/test_deployed_fixes.py`** — 10 new regression tests added (Session 3): `TestSession3EnvironmentScorerColdStart`, `TestSession3BlendedConfidence`, `TestSession3NasdaqYahooMap`, `TestSession3OhlcBootstrapStagger`, `TestSession3StartupCleanup`. Total: 30 tests.

---

## Session 4 — 2026-06-05

### Intelligent trailing stop, breakeven lock, and limit extension

**What existed before this session:**
- `_apply_breakeven` in `trading/trade_manager.py`: moves stop to entry when profit ≥ `breakeven_trigger_points` (30 pts fixed). Fully functional.
- `_apply_trailing` in `trading/trade_manager.py`: trails stop at ATR-based distance (set at entry via `get_trail_distance`). Trigger was fixed at `adaptive_trailing_trigger_points` (50 pts) regardless of instrument ATR.
- `_sync_stop_to_ig` in `trading/trade_manager.py`: pushes updated stop to IG via `PUT /positions/otc/{deal_id}`. `broker_stop_management=True` was already wired in `ExecutionEngine` for live/demo modes. The IG REST `update_position_stops` method existed and worked.
- **Gap 1**: Trail and breakeven triggers were fixed points — too large for small-ATR instruments, too small for large-ATR (Nikkei/Dow) positions.
- **Gap 2**: No mechanism to extend the take-profit limit as a trend continued.
- **Gap 3**: `_sync_stop_to_ig` always did an extra GET to preserve the current IG limit level; limit updates required a separate, unimplemented path.

**Changes made:**

- **`config/config_v25.json`** — Added `"trailing_stop"` block with six new keys:
  - `trail_trigger_atr_multiple` (default `1.0`): start trailing when profit ≥ N × entry ATR; 0 = use `adaptive_trailing_trigger_points`.
  - `breakeven_trigger_atr_multiple` (default `0.5`): move stop to breakeven when profit ≥ N × entry ATR; 0 = use `breakeven_trigger_points`.
  - `limit_extension_enabled` (default `false`): opt-in flag for limit extension.
  - `limit_extension_trigger_atr_multiple` (default `1.5`): min profit before first extension.
  - `limit_extension_step_atr_multiple` (default `1.0`): how far to push limit per extension.
  - `limit_extension_max_extensions` (default `2`): cap on extensions per position (in-memory).

- **`src/system/config.py`** — Added eight new `@property` accessors for the `trailing_stop` block (`trailing_stop`, `trail_trigger_atr_multiple`, `breakeven_trigger_atr_multiple`, `limit_extension_enabled`, `limit_extension_trigger_atr_multiple`, `limit_extension_step_atr_multiple`, `limit_extension_max_extensions`).

- **`src/data/learning_store.py`** — Added `update_target(trade_id, target, note)` to update the take-profit level in the DB (mirrors existing `update_stop`).

- **`src/trading/trade_manager.py`** — Core logic changes:
  - Added `_last_ig_limit: dict[str, float]` and `_limit_ext_count: dict[int, int]` instance state.
  - Added `_effective_trail_trigger(entry_atr)`: returns `trail_trigger_atr_multiple × ATR` when ATR > 0, else `cfg.trailing_stop_trigger_points`.
  - Added `_effective_breakeven_trigger(entry_atr)`: same pattern for breakeven.
  - Added `_apply_limit_extension(market, side, trade_id, entry, current_target, px, entry_atr)`: extends limit by `step_atr × ATR` each time profit exceeds the next threshold, up to `max_extensions` times. Persists to DB and logs.
  - `update_from_quote`: now uses `_effective_breakeven_trigger` and `_effective_trail_trigger` to compute ATR-scaled thresholds (backwards-compatible — zero-config fallback to existing points). Also calls `_apply_limit_extension` when enabled, and passes `new_limit` to `_sync_stop_to_ig` when limit moved.
  - `_sync_stop_to_ig`: added optional `new_limit` keyword arg. When provided, uses it directly in the `PUT` payload instead of doing an extra GET to read current IG limit — saves one REST call per trailing update. Deduplicates limit pushes via `_last_ig_limit` cache.

- **`tests/test_trade_manager.py`** — 7 new tests across two new test classes:
  - `ATRBasedTriggerTests`: `test_atr_breakeven_fires_before_fixed_trigger`, `test_atr_trail_trigger_overrides_fixed_points`, `test_atr_trigger_zero_falls_back_to_points`.
  - `LimitExtensionTests`: `test_limit_extension_fires_on_trigger`, `test_limit_extension_capped_at_max`, `test_limit_extension_not_fired_when_disabled`, `test_sell_limit_extension_moves_target_down`.

**Dashboard**: No rebuild needed — no new position snapshot fields were added. The existing `target`/`trail_active`/`breakeven_hit` fields in the position row automatically reflect updated values.

---

## Session 4 — 2026-06-05 — Performance tuning pass (Nasdaq star session)

### Context
Nasdaq (US Tech 100) producing multiple profitable SELL trades. Points engine at cumulative=3.0, state=CAUTION (needs >6 for HEALTHY). This session maximises live performance by enabling limit extension and lifting the CAUTION size floor.

### Changes

**`config/config_v25.json` — trailing_stop block tuned for Nasdaq ATR (80–150 pts / 5-min bar)**
- `limit_extension_enabled`: `false` → **`true`** — activates the limit-extension mechanism built in Session 3.
- `trail_trigger_atr_multiple`: `1.0` → **`0.75`** — begin trailing sooner (at 75% of entry ATR profit, ~60–112 pts). Reduces the chance of giving back gains before the trail activates.
- `breakeven_trigger_atr_multiple`: `0.5` → **`0.4`** — move stop to breakeven earlier (at 40% of entry ATR, ~32–60 pts). Locks in profits faster on volatile Nasdaq moves.
- `limit_extension_step_atr_multiple`: `1.0` → **`0.75`** — smaller per-extension step means more frequent extensions on a trending move; still ~60–112 pts per step for Nasdaq.
- `limit_extension_max_extensions`: `2` → **`3`** — allows up to 3 limit extensions per position (was 2), giving trends more room to run.

**`src/trading/points_engine.py` — CAUTION size multiplier lifted**
- `get_size_multiplier` CAUTION branch: removed the 80–88% lower band returning `0.25×`. Now returns **`0.5×`** for all `conf >= CONF_MARGINAL_MIN (80%)` in CAUTION state.
  - Old behaviour: conf 80–87% → 0.25×, conf ≥ 88% → 0.5×.
  - New behaviour: conf ≥ 80% → 0.5× flat.
  - Rationale: Nasdaq `trade_size=3`, so 0.25× = 0.75 effective (barely above IG min). 0.5× = 1.5, a meaningful position that reflects actual confidence. Agent firing at 85–99% in CAUTION was being unnecessarily penalised at 0.25× in the 85–88% band.
- `min_size_confidence_threshold` CAUTION return: `88.0` → **`CONF_MARGINAL_MIN (80.0)`** — consistent with the new flat multiplier.

**`src/trading/trading_loop.py`** — Updated stale comment on line 1064 to reflect new CAUTION 0.5× flat floor.

**`tests/test_points_engine.py`** — Updated two tests to reflect the new CAUTION multiplier behaviour:
- `test_min_size_confidence_threshold_caution_is_88` → renamed and updated to expect `80.0`.
- `test_size_multiplier_caution_bands` and `test_size_multiplier_spec_matrix` — `82.0` confidence now asserts `0.5` (was `0.25`).

### Confirmed unchanged (audited)
- HEALTHY threshold (>6.0 pts): proportional — a high-confidence £200 win scores ~3 pts; takes 2–3 wins. Correct.
- Nasdaq `trade_size=3`, `risk_cap_gbp=500`: effective size 1.5 at 0.5× CAUTION, risk £150/trade — well within cap.
- `rsi_buy_max=85`, `rsi_sell_min=15`: relaxed RSI filters already in config. ✓
- `max_open_positions=15`: not a bottleneck. ✓
- `COLD_START_BARS=2` in `session_manager.py`: confirmed unchanged. ✓
- Nasdaq `trading_session_whitelist`: `london_morning`, `london_us_overlap`, `us_afternoon`. ✓
- Nasdaq confidence effective floor: `max(80, signal_threshold=75)` = 80 — agent firing at 85–99% well above floor. ✓

---

## Session 4 — 2026-06-05 — Pre-launch validation & final fixes

### Context
All open positions closed. Agent being restarted for a fresh live session. This entry covers the dashboard positions fix, version splash fix, and the full pre-launch test suite added before relaunch.

### Changes

**`src/api/snapshot_store.py` — Dashboard positions aggregation**
- `_tick_for_readers`: added position aggregation loop — iterates `tick["markets"]` and hoists each market's `positions` list into a flat top-level `positions` array. Each entry is enriched with `epic` and `market` keys (fallback to epic if no `market_name`). This ensures the dashboard `TradesPanel` always has a populated `positions` list to render even when the trading loop publishes positions nested under the market slice.

**`dashboard/src/components/TradesPanel.jsx` — Positions resolver fallback**
- `resolvePositions`: added fallback chain — tries `tick.positions` first (top-level, from `_tick_for_readers`), then `tick.markets[epic].positions` for single-epic views, so the panel renders in both architectures without change to the snapshot format.

**`src/data/version.json` — Structured changelog format**
- Changed from a flat version string to a structured object `{version, date, title, changes:[]}`. The splash screen now reads `title` and `changes` to render a human-readable "What's new" section at startup.

**`config/config_v25.json` — Trailing stop parameters finalised (live session)**
- `trail_trigger_atr_multiple`: tuned to `0.75` — begin trailing at 75% of entry ATR profit.
- `breakeven_trigger_atr_multiple`: tuned to `0.4` — move stop to breakeven at 40% of entry ATR profit.
- `limit_extension_enabled`: `true` — limit extension active for Nasdaq star session.
- `limit_extension_max_extensions`: `3` — up to 3 extensions per position.
- `limit_extension_step_atr_multiple`: `0.75` — smaller step for more frequent extensions on trending moves.
- `rsi_buy_max`: `85`, `rsi_sell_min`: `15` — relaxed RSI bounds for wider entry window.
- `COLD_START_BARS`: `2` — fast cold-start (10 min vs 60 min) for intraday session restarts.
- `london_morning` session added to `wall_street` and `nasdaq_100` whitelists.

**`src/trading/points_engine.py` — CAUTION size multiplier flat**
- CAUTION branch of `get_size_multiplier` now returns `0.5×` for all `conf >= CONF_MARGINAL_MIN (80%)`. Previously returned `0.25×` in the 80–87% band, `0.5×` above 88%.
- `min_size_confidence_threshold` for CAUTION updated to return `CONF_MARGINAL_MIN (80.0)` (was `88.0`).

**`src/trading/trading_loop.py` — Position laddering**
- `_dynamic_max_per_epic(base_cap, open_count, tracker)`: new method. Returns `base_cap` when points state is not HEALTHY. Increments to `base_cap+1` when all open positions on the epic have `pnl_gbp > 0`, and `base_cap+2` when additionally the oldest position is ≥ 20 minutes old. Guards against stacking into new green positions and against adding to losing moves.

### Tests added — `tests/test_deployed_fixes.py`
New class `TestSession4PreLaunchValidation` with 9 tests:

| Test | What it proves |
|---|---|
| `test_points_engine_records_trade_and_updates_state` | PointsEngine cumulative rises and transitions to HEALTHY after 8 profitable wins |
| `test_points_engine_caution_size_multiplier_flat` | CAUTION state returns 0.5× for conf 80, 85, 88, 95 — no more 0.25× split |
| `test_trailing_stop_config_keys_present` | `trailing_stop` block in config has all 4 required keys including `limit_extension_enabled=True` and `limit_extension_max_extensions=3` |
| `test_trailing_stop_atr_trigger_scales` | `trail_trigger_atr_multiple` is a fractional multiple < 1.0; `_effective_trail_trigger` uses `mult × ATR` |
| `test_dynamic_max_per_epic_healthy_required` | CAUTION → base_cap; HEALTHY+profitable+young → base_cap+1; HEALTHY+profitable+mature → base_cap+2 |
| `test_snapshot_positions_aggregated_from_markets` | `_tick_for_readers` aggregates positions from `markets[epic].positions` into top-level list with `epic` and `market` keys |
| `test_ml_blend_confidence_capped_at_100` | Blend formula `(rules×0.6)+(ml×100×0.4)` clamped to 100; source contains `min(100.0, conf)` |
| `test_market_weakness_detection` | `_gate_environment_fitness` with score=20% returns `passed=False` with "fitness" in detail |
| `test_agent_blocks_on_low_fitness_not_confidence` | score=25% blocks regardless of confidence level; gate value reports score below GATE_PASS_MIN |

### Suite result
428 tests passed, 0 failures, 25 deprecation warnings (datetime.utcnow — pre-existing).

---

## Session 5 — 2026-06-05 (v25.2.2)

### Critical fix: correct IG contract point values

**Root cause:** `ig_point_value_gbp` was set to 1.0 for all instruments. The real IG CFD contract multipliers are much larger, causing the agent to underestimate P&L and risk by up to 15× for Nasdaq.

**Evidence:** Live IG P&L data: -£241.96 for a 16.2pt adverse move on Nasdaq at size=1.0 → £241.96 / 16.2 = **£14.94/pt**.

**Changes — `config/config_v25.json`:**

| Instrument | Epic | Old £/pt | New £/pt | Basis |
|---|---|---|---|---|
| Nasdaq 100 | IX.D.NASDAQ.IFM.IP | 1.0 | **14.94** | Live IG P&L confirmed |
| Wall Street | IX.D.DOW.IFM.IP | 1.0 | **7.87** | $10/pt ÷ 1.27 GBPUSD |
| Spot Gold | CS.D.CFPGOLD.CFP.IP | 1.0 | **0.79** | $1/pt ÷ 1.27 GBPUSD |
| Japan 225 | IX.D.NIKKEI.IFM.IP | 1.0 | **5.13** | ¥1000/pt ÷ 195 GBPJPY |

**Position sizes recalculated for ~£50 risk/trade target (size × £/pt × stop_pts = £50):**

| Instrument | Stop pts | Old size | New size | Risk/trade |
|---|---|---|---|---|
| Nasdaq 100 | 100 | 3.0 | **0.05** | £74.70 |
| Wall Street | 80 | 10 | **0.1** | £62.96 |
| Spot Gold | 10 | 5 | **6.0** | £47.40 |
| Japan 225 | 45 | 10 | **0.2** | £46.17 |

**Additional changes:**
- `adaptive_min_trade_size` lowered from 0.5 → **0.01** — previously clamped Nasdaq/Dow/Nikkei sizes up to 0.5, which would have meant £373.50 risk/trade for Nasdaq at the adaptive floor alone.
- `risk_cap_gbp` per instrument updated to reflect actual max exposure (Nasdaq: 150, Wall Street: 150, Gold: 200, Japan 225: 100).
- `version` in config_v25.json bumped to `25.2.2`.
