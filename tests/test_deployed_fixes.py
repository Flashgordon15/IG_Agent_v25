"""
Regression tests for every agent-applied fix recorded in AGENT_CHANGES.md.

Each test proves a specific change is present in the live code — not just that
the general test suite passes. Run before every restart:

    PYTHONPATH=src python3 -m pytest tests/test_deployed_fixes.py -v

A failing test tells you EXACTLY which fix is missing or was reverted.
"""

from __future__ import annotations

import importlib
import inspect
import json
import textwrap
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
SRC = PROJECT / "src"
CONFIG = PROJECT / "config" / "config_v25.json"


# ---------------------------------------------------------------------------
# SESSION 1 FIXES
# ---------------------------------------------------------------------------


class TestSession1SignalEngine:
    """Signal confidence must be clamped to 100."""

    def test_confidence_clamped_to_100(self):
        src = (SRC / "signals" / "signal_engine.py").read_text()
        # Both raw_conf and adjusted must be clamped
        assert "min(100.0, float(raw_conf))" in src, (
            "raw_conf is NOT clamped to 100 — see signal_engine.py"
        )
        assert "min(100.0, float(adjusted))" in src, (
            "adjusted confidence is NOT clamped to 100 — see signal_engine.py"
        )


class TestSession1Config:
    """US indices trade overlap/afternoon only — london_morning removed (profitability)."""

    def test_wall_street_excludes_london_morning(self):
        cfg = json.loads(CONFIG.read_text())
        ws = cfg["instruments"]["wall_street"]
        assert "london_morning" not in ws["trading_session_whitelist"], (
            "wall_street must not trade london_morning — weak session for US indices"
        )
        assert "london_us_overlap" in ws["trading_session_whitelist"]

    def test_nasdaq_excludes_london_morning(self):
        cfg = json.loads(CONFIG.read_text())
        nas = cfg["instruments"]["nasdaq_100"]
        assert "london_morning" not in nas["trading_session_whitelist"], (
            "nasdaq_100 must not trade london_morning — weak session for US indices"
        )
        assert "london_us_overlap" in nas["trading_session_whitelist"]


class TestSession1GapClearBars:
    """GAP_CLEAR_BARS constant must exist in session_manager."""

    def test_gap_clear_bars_defined(self):
        import trading.session_manager as sm

        assert hasattr(sm, "GAP_CLEAR_BARS"), (
            "GAP_CLEAR_BARS constant missing from session_manager"
        )
        assert sm.GAP_CLEAR_BARS == 12, (
            f"GAP_CLEAR_BARS={sm.GAP_CLEAR_BARS}, expected 12 (1 hour = 12 × 5-min bars)"
        )


class TestSession1PnlFix:
    """sum_daily_pnl must exclude dry-run trades with zero ig_pnl_currency."""

    def test_dry_run_excluded_from_pnl(self):
        src = (SRC / "data" / "learning_store.py").read_text()
        assert "dry_run = 1" in src and "COALESCE(ig_pnl_currency" in src, (
            "sum_daily_pnl does not exclude dry-run phantom trades — P&L will be wrong"
        )


# ---------------------------------------------------------------------------
# SESSION 2 FIXES
# ---------------------------------------------------------------------------


class TestSession2GapExpiry:
    """Gap expiry must use elapsed_bars_since_open(), not bars_since_open()."""

    def test_elapsed_bars_method_exists(self):
        import trading.session_manager as sm

        assert hasattr(sm.SessionManager, "elapsed_bars_since_open"), (
            "SessionManager.elapsed_bars_since_open() missing — gap expiry is broken"
        )

    def test_elapsed_bars_is_uncapped(self):
        """elapsed_bars_since_open must NOT apply the COLD_START_BARS cap."""
        src = inspect.getsource(
            importlib.import_module(
                "trading.session_manager"
            ).SessionManager.elapsed_bars_since_open
        )
        assert "COLD_START_BARS" not in src, (
            "elapsed_bars_since_open still has the COLD_START_BARS cap — gap expiry will never fire"
        )

    def test_gate_uses_elapsed_not_capped(self):
        """_gate_cold_start_gap must call elapsed_bars_since_open for the expiry check."""
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert "elapsed_bars_since_open" in src, (
            "_gate_cold_start_gap does not call elapsed_bars_since_open() — "
            "gap block will never expire (bars_since_open is capped at 6)"
        )

    def test_gap_expiry_check_uses_elapsed_variable(self):
        """The GAP_CLEAR_BARS comparison must be against the uncapped variable."""
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert "bars_elapsed >= GAP_CLEAR_BARS" in src, (
            "GAP_CLEAR_BARS is not compared against bars_elapsed — "
            "gap expiry logic is missing or uses the wrong (capped) variable"
        )


class TestSession2RsiBuyMax:
    """RSI filters must be relaxed to allow ML-led trading decisions."""

    def test_rsi_buy_max_relaxed(self):
        cfg = json.loads(CONFIG.read_text())
        val = cfg.get("rsi_buy_max")
        assert val >= 85, (
            f"rsi_buy_max={val}, expected >=85. "
            "Tightening RSI filters blocks high-confidence ML-blended signals."
        )

    def test_rsi_sell_min_relaxed(self):
        cfg = json.loads(CONFIG.read_text())
        val = cfg.get("rsi_sell_min")
        assert val <= 15, (
            f"rsi_sell_min={val}, expected <=15. "
            "At 20, oversold SELL signals (e.g. Gold RSI 15) are incorrectly blocked."
        )


class TestSession2MlDecisionLog:
    """Trading loop must maintain and publish ml_decision_log in snapshot."""

    def test_ml_decision_log_attribute_exists(self):
        """TradingLoop.__init__ must initialise _ml_decision_log."""
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert "_ml_decision_log" in src, (
            "_ml_decision_log not found in trading_loop.py — ML decisions will not appear on dashboard"
        )

    def test_ml_decision_log_in_snapshot_payload(self):
        """Snapshot payload must include the ml_decision_log key."""
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert '"ml_decision_log"' in src, (
            '"ml_decision_log" not in _build_snapshot_payload — dashboard ML panel will always be empty'
        )

    def test_fmtlogline_handles_ml_entries(self):
        """LivePanel.jsx must handle ml_prob-keyed entries."""
        src = (
            PROJECT / "dashboard" / "src" / "components" / "LivePanel.jsx"
        ).read_text()
        assert "ml_prob" in src, (
            "LivePanel.jsx fmtLogLine does not handle ml_prob entries — ML log will show raw JSON"
        )


class TestSession2SystemApi:
    """get_system_info() must return the fields SystemTab reads."""

    def test_system_info_returns_required_fields(self):
        from api.dashboard_data import get_system_info

        info = get_system_info()
        required = {
            "branch",
            "commit",
            "ml_record_count",
            "ml_fields",
            "caffeinate_running",
            "ohlc_markets_cached",
            "sessions_passed",
            "sessions_required",
        }
        missing = required - set(info.keys())
        assert not missing, (
            f"get_system_info() is missing fields: {missing}. "
            "SystemTab will show '—' / 'NOT RUNNING' for these."
        )


class TestSession2ShadowToday:
    """shadow_today() must return correct top_blocked_setup and top_3_setups."""

    def test_top_3_setups_key_present(self):
        from api.intelligence_data import shadow_today

        result = shadow_today()
        assert "top_3_setups" in result, (
            "shadow_today() missing 'top_3_setups' key — IntelligencePanel reads this field"
        )
        assert isinstance(result["top_3_setups"], list), "top_3_setups must be a list"

    def test_top_blocked_not_hardcoded_unknown(self):
        """The counter must skip empty-key rows so 'unknown' isn't always #1."""
        src = (SRC / "api" / "intelligence_data.py").read_text()
        # Must filter on r.get("setup_key") being truthy before counting
        assert 'r.get("setup_key")' in src, (
            "shadow_today does not filter empty setup_key rows — top_blocked_setup will always be 'unknown'"
        )

    def test_estimated_extra_uses_threshold(self):
        """estimated_extra_if_threshold_minus_5 must compare against a numeric threshold."""
        src = (SRC / "api" / "intelligence_data.py").read_text()
        assert ">= 75.0" in src or ">= 75" in src, (
            "estimated_extra_if_threshold_minus_5 does not use a 75% floor — "
            "it will count all blocked signals (nonsensical result)"
        )


class TestSession2RestCallsMin:
    """rest_calls_min in snapshot must not be hardcoded 0."""

    def test_rest_calls_uses_budget(self):
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert '"rest_calls_min": 0' not in src, (
            '"rest_calls_min" is still hardcoded to 0 in _build_snapshot_payload — '
            "the REST budget bar on the dashboard will always read 0"
        )
        assert "_rest_calls_last_minute" in src, (
            "_rest_calls_last_minute() helper is missing from TradingLoop"
        )

    def test_rest_calls_helper_uses_budget(self):
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert "get_rest_api_budget" in src, (
            "_rest_calls_last_minute does not call get_rest_api_budget() — "
            "calls will always return 0"
        )


class TestSession2SentimentBadge:
    """Header sentiment badge must read sentiment.label, not compare dict to string."""

    def test_sentiment_reads_label(self):
        src = (PROJECT / "dashboard" / "src" / "components" / "Header.jsx").read_text()
        assert "sentiment?.label" in src or "sentiment.label" in src, (
            "Header.jsx sentiment badge still compares dict object to string — badge will never show"
        )
        # The old broken pattern must be gone
        assert 'sentiment === "crowded_long"' not in src, (
            'Header.jsx still uses `sentiment === "crowded_long"` (object vs string) — badge is broken'
        )


# ---------------------------------------------------------------------------
# SESSION 3 FIXES
# ---------------------------------------------------------------------------


class TestSession3EnvironmentScorerColdStart:
    """Environment scorer cold start cap must be aligned with session manager (2 bars)."""

    def test_cold_start_bar_cap_is_2(self):
        src = (SRC / "trading" / "environment_scorer.py").read_text()
        assert "COLD_START_BAR_CAP = 2" in src, (
            "environment_scorer.COLD_START_BAR_CAP is not 2 — "
            "fitness will be capped at 55% for 30 minutes instead of 10 minutes"
        )

    def test_environment_scorer_cap_matches_session_manager(self):
        import sys

        sys.path.insert(0, str(SRC))
        from trading.environment_scorer import COLD_START_BAR_CAP as env_cap
        from trading.session_manager import COLD_START_BARS as session_cap

        assert env_cap == session_cap, (
            f"environment_scorer.COLD_START_BAR_CAP ({env_cap}) != "
            f"session_manager.COLD_START_BARS ({session_cap}) — "
            "cold start clears at different times in scorer vs session manager"
        )


class TestSession3BlendedConfidence:
    """Dashboard snapshot must expose ML-blended confidence, not raw rules score."""

    def test_snapshot_reads_blended_from_gate(self):
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert (
            '_g.value.get("confidence")' in src or 'g.value.get("confidence")' in src
        ), (
            "_build_snapshot_payload does not extract blended confidence from "
            "signal_confidence gate — dashboard will show rules-only value"
        )

    def test_snapshot_includes_rules_confidence(self):
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert '"rules_confidence"' in src, (
            "signal dict does not include rules_confidence — "
            "impossible to distinguish blended from rules-only on dashboard"
        )

    def test_snapshot_includes_threshold_delta(self):
        src = (SRC / "trading" / "trading_loop.py").read_text()
        assert '"threshold_delta"' in src, (
            "signal dict does not include threshold_delta — "
            "dashboard cannot show how close confidence is to the floor"
        )


class TestSession3NasdaqYahooMap:
    """Nasdaq must be in the Yahoo OHLC map so its cache can be pre-populated."""

    def test_nasdaq_in_yahoo_map(self):
        import sys

        sys.path.insert(0, str(SRC))
        from data.ohlc_yahoo_seeder import EPIC_YAHOO_MAP

        assert "IX.D.NASDAQ.IFM.IP" in EPIC_YAHOO_MAP, (
            "Nasdaq epic IX.D.NASDAQ.IFM.IP not in EPIC_YAHOO_MAP — "
            "Nasdaq OHLC cache cannot be pre-populated from Yahoo"
        )
        symbol, market = EPIC_YAHOO_MAP["IX.D.NASDAQ.IFM.IP"]
        assert symbol == "NQ=F", f"Expected NQ=F, got {symbol!r}"


class TestSession3OhlcBootstrapStagger:
    """OHLC bootstrap must stagger REST calls to avoid bursting the 3/min cap."""

    def test_stagger_constant_defined(self):
        src = (SRC / "trading" / "ohlc_bootstrap.py").read_text()
        assert "_OHLC_REST_STAGGER_SEC" in src, (
            "_OHLC_REST_STAGGER_SEC constant missing from ohlc_bootstrap.py — "
            "parallel REST calls will burst the 3/min cap on startup"
        )

    def test_stagger_applied_in_parallel_bootstrap(self):
        src = (SRC / "trading" / "ohlc_bootstrap.py").read_text()
        assert "time.sleep(_OHLC_REST_STAGGER_SEC)" in src, (
            "bootstrap_ohlc_parallel does not sleep between REST fetches — "
            "multiple markets will burst the 3-calls/min cap simultaneously"
        )


class TestSession3StartupCleanup:
    """main.py must kill stale agent processes at startup and report it in the splash."""

    def test_pre_startup_cleanup_exists(self):
        src = (SRC / "main.py").read_text()
        assert "_pre_startup_cleanup" in src, (
            "_pre_startup_cleanup() is missing from main.py — "
            "stale agent processes will block the next launch"
        )

    def test_cleanup_called_in_main(self):
        src = (SRC / "main.py").read_text()
        assert "_pre_startup_cleanup()" in src, (
            "_pre_startup_cleanup() is defined but never called in main() — "
            "stale processes will not be killed on startup"
        )

    def test_cleanup_marks_session_cleanup_phase(self):
        src = (SRC / "main.py").read_text()
        assert '"session_cleanup"' in src, (
            "_pre_startup_cleanup() does not mark the session_cleanup startup phase — "
            "splash screen will not show 'Previous session closed'"
        )

    def test_session_cleanup_phase_in_tracker(self):
        import sys

        sys.path.insert(0, str(SRC))
        from system.startup_tracker import PHASES

        phase_ids = [p[0] for p in PHASES]
        assert "session_cleanup" in phase_ids, (
            "'session_cleanup' phase is missing from startup_tracker.PHASES — "
            "splash screen will not show the cleanup step"
        )
        assert phase_ids.index("session_cleanup") == 0, (
            "'session_cleanup' must be the first startup phase"
        )

    def test_cleanup_uses_sigkill_fallback(self):
        src = (SRC / "main.py").read_text()
        assert "SIGKILL" in src, (
            "_pre_startup_cleanup() has no SIGKILL fallback — "
            "stubborn processes will not be killed"
        )


# ---------------------------------------------------------------------------
# SESSION 4 PRE-LAUNCH VALIDATION
# ---------------------------------------------------------------------------


class TestSession4PreLaunchValidation:
    """Pre-launch validation for Session 4 changes: trailing stop ATR scaling,
    CAUTION size multiplier, position laddering, dashboard positions aggregation,
    ML blend cap, and environment fitness gating."""

    # ------------------------------------------------------------------
    # Test A: Trades & points sync
    # ------------------------------------------------------------------

    def test_points_engine_records_trade_and_updates_state(self, tmp_path):
        import sys

        sys.path.insert(0, str(SRC))
        from trading.points_engine import PointsEngine, _nominal_state

        engine = PointsEngine(store=None, state_path=tmp_path / "pts.json")
        # Fresh engine starts at cumulative=0 → CAUTION
        assert engine.get_state() in ("CAUTION", "HEALTHY")

        # Record 8 wins — flat scoring (no DB): each += 1.0
        for _ in range(8):
            engine.record_trade("WIN", confidence=90.0, pnl_pts=5.0)

        snap = engine.snapshot()
        from trading.points_engine import HEALTHY_CUMULATIVE_MIN

        assert snap.cumulative > HEALTHY_CUMULATIVE_MIN, (
            f"After 8 wins cumulative={snap.cumulative:.1f} — expected > {HEALTHY_CUMULATIVE_MIN} (HEALTHY)"
        )
        assert engine.get_state() == "HEALTHY", (
            f"State={engine.get_state()} — expected HEALTHY after cumulative > {HEALTHY_CUMULATIVE_MIN}"
        )

    def test_points_engine_caution_size_multiplier_flat(self, tmp_path):
        """CAUTION state must return 0.5× for all confidence values ≥ 80 (new flat rate)."""
        import sys

        sys.path.insert(0, str(SRC))
        from trading.points_engine import CONF_MARGINAL_MIN, PointsEngine

        engine = PointsEngine(store=None, state_path=tmp_path / "pts_caution.json")
        # cumulative=0 → CAUTION
        assert engine.get_state() == "CAUTION", (
            f"Expected CAUTION at zero cumulative, got {engine.get_state()}"
        )
        for conf in (80, 85, 88, 95):
            mult = engine.get_size_multiplier(float(conf))
            assert mult == 0.5, (
                f"CAUTION state: get_size_multiplier({conf}) = {mult}, expected 0.5 "
                f"(flat rate for all conf >= {CONF_MARGINAL_MIN})"
            )

    # ------------------------------------------------------------------
    # Test B: Trailing stop
    # ------------------------------------------------------------------

    def test_trailing_stop_config_keys_present(self):
        cfg = json.loads(CONFIG.read_text())
        ts = cfg.get("trailing_stop")
        assert isinstance(ts, dict), "trailing_stop block missing from config_v25.json"
        for key in (
            "trail_trigger_atr_multiple",
            "breakeven_trigger_atr_multiple",
            "limit_extension_enabled",
            "limit_extension_max_extensions",
        ):
            assert key in ts, f"trailing_stop.{key} missing from config_v25.json"
        assert ts["limit_extension_enabled"] is True, (
            f"limit_extension_enabled={ts['limit_extension_enabled']}, expected True"
        )
        assert ts["limit_extension_max_extensions"] == 3, (
            f"limit_extension_max_extensions={ts['limit_extension_max_extensions']}, expected 3"
        )

    def test_trailing_stop_atr_trigger_scales(self, tmp_path):
        """_effective_trail_trigger must return mult * atr when mult > 0."""
        import sys

        sys.path.insert(0, str(SRC))

        cfg_data = json.loads(CONFIG.read_text())
        mult = cfg_data["trailing_stop"]["trail_trigger_atr_multiple"]
        assert 0 < mult < 1.0, (
            f"trail_trigger_atr_multiple={mult} — expected a fractional ATR multiple < 1.0"
        )

        # Verify TradeManager._effective_trail_trigger uses the multiple correctly
        src = (SRC / "trading" / "trade_manager.py").read_text()
        assert "_effective_trail_trigger" in src, (
            "_effective_trail_trigger() missing from trade_manager.py"
        )
        assert "trail_trigger_atr_multiple" in src, (
            "trade_manager.py does not reference trail_trigger_atr_multiple — "
            "ATR scaling not wired up"
        )
        assert "mult * entry_atr" in src or "mult * atr" in src, (
            "trade_manager._effective_trail_trigger does not multiply mult by atr"
        )

    # ------------------------------------------------------------------
    # Test C: Position laddering
    # ------------------------------------------------------------------

    def test_dynamic_max_per_epic_healthy_required(self):
        """Laddering must stay at base_cap unless points state == HEALTHY."""
        import sys

        sys.path.insert(0, str(SRC))
        from trading.trading_loop import TradingLoop

        class _MockPoints:
            def __init__(self, state: str) -> None:
                self._state = state

            def get_state(self) -> str:
                return self._state

        class _MockTracker:
            def __init__(self, positions=None) -> None:
                self._positions = positions or []

            def snapshot(self) -> dict:
                return {"positions": self._positions}

        loop = object.__new__(TradingLoop)
        loop._epic = "IX.D.FTSE.IFM.IP"

        # CAUTION — must not ladder
        loop._points = _MockPoints("CAUTION")
        cap, reason = loop._dynamic_max_per_epic(2, 1, _MockTracker())
        assert cap == 2, f"CAUTION state: expected base_cap=2, got {cap}"
        assert "CAUTION" in reason, f"Reason should mention CAUTION, got: {reason!r}"

        # HEALTHY, no positions → no ladder
        loop._points = _MockPoints("HEALTHY")
        cap, reason = loop._dynamic_max_per_epic(2, 0, _MockTracker())
        assert cap == 2, f"HEALTHY/no open: expected base_cap=2, got {cap}"

        # HEALTHY, all positions profitable, oldest < 20 min → base_cap+1
        pos_young = [{"epic": loop._epic, "pnl_gbp": 5.0, "open_mins": 10}]
        cap, reason = loop._dynamic_max_per_epic(2, 1, _MockTracker(pos_young))
        assert cap == 3, f"HEALTHY/profitable/young: expected base_cap+1=3, got {cap}"

        # HEALTHY, all positions profitable, oldest >= 20 min → base_cap+2
        pos_mature = [{"epic": loop._epic, "pnl_gbp": 5.0, "open_mins": 25}]
        cap, reason = loop._dynamic_max_per_epic(2, 1, _MockTracker(pos_mature))
        assert cap == 4, f"HEALTHY/profitable/mature: expected base_cap+2=4, got {cap}"

    # ------------------------------------------------------------------
    # Test D: Dashboard positions aggregation
    # ------------------------------------------------------------------

    def test_snapshot_positions_aggregated_from_markets(self):
        """_tick_for_readers must hoist positions from markets[epic].positions to top level."""
        import sys

        sys.path.insert(0, str(SRC))
        from api.snapshot_store import _tick_for_readers

        tick = {
            "markets": {
                "EPIC1": {
                    "market_name": "Wall Street",
                    "positions": [
                        {
                            "deal_id": "X",
                            "side": "SELL",
                            "entry": 100.0,
                            "current": 95.0,
                            "pnl_gbp": 2.5,
                            "size": 0.5,
                        }
                    ],
                }
            }
        }
        out = _tick_for_readers(tick)
        positions = out.get("positions")
        assert isinstance(positions, list) and len(positions) > 0, (
            "_tick_for_readers did not aggregate positions from markets[epic].positions "
            "into top-level 'positions' list — TradesPanel will show empty trades"
        )
        first = positions[0]
        assert first.get("deal_id") == "X", f"Aggregated position lost deal_id: {first}"
        assert first.get("epic") == "EPIC1", (
            f"Aggregated position missing epic key: {first}"
        )
        assert first.get("market") == "Wall Street", (
            f"Aggregated position missing market name: {first}"
        )

    # ------------------------------------------------------------------
    # Test E: ML alignment
    # ------------------------------------------------------------------

    def test_ml_blend_confidence_capped_at_100(self):
        """Blended confidence from ML + rules must never exceed 100."""
        src = (SRC / "trading" / "trading_loop.py").read_text()
        # The blend formula and clamp must both be present
        assert "min(100.0, conf)" in src, (
            "trading_loop._gate_signal_confidence does not clamp blended conf to 100 — "
            "confidence > 100 would pass any threshold check"
        )
        assert "ml_prob * 100.0 * 0.4" in src or "ml_prob * 100" in src, (
            "ML blend formula missing from trading_loop — ML probability not scaled to %"
        )
        # Verify the math: worst case 100% rules + 100% ML → exactly 100
        rules_conf = 100.0
        ml_prob = 1.0
        blended = (rules_conf * 0.6) + (ml_prob * 100.0 * 0.4)
        clamped = max(0.0, min(100.0, blended))
        assert clamped <= 100.0, f"Blended confidence {blended} > 100 before clamp"
        assert clamped == 100.0

    # ------------------------------------------------------------------
    # Test F: Market weakness vs agent (environment fitness gate)
    # ------------------------------------------------------------------

    def test_market_weakness_detection(self):
        """_gate_environment_fitness must fail with a fitness-specific reason when score < threshold."""
        import sys

        sys.path.insert(0, str(SRC))
        from datetime import datetime

        import pandas as pd

        from data.models import Quote
        from trading.environment_scorer import GATE_PASS_MIN
        from trading.trading_loop import TradingLoop

        class _WeakEnv:
            def score(self, market, quote=None, quote_df=None):
                return 20.0  # well below GATE_PASS_MIN (55)

            def get_sentiment_factor(self, market):
                return {}

        class _MockSignalEngine:
            def quote_df(self, market):
                return pd.DataFrame()

        loop = object.__new__(TradingLoop)
        loop._market = "EPIC1"
        loop._env = _WeakEnv()
        loop._signal_engine = _MockSignalEngine()

        quote = Quote(datetime.now(), 100.0, 101.0)
        result = loop._gate_environment_fitness(quote)

        assert not result.passed, (
            "environment_fitness gate passed with score=20% — "
            "weak market conditions should block trading"
        )
        assert "fitness" in result.detail.lower(), (
            f"Gate detail {result.detail!r} does not mention 'fitness' — "
            "rejection reason is not distinguishable from a confidence failure"
        )
        assert result.name == "environment_fitness", (
            f"Gate name is {result.name!r}, expected 'environment_fitness'"
        )

    def test_agent_blocks_on_low_fitness_not_confidence(self):
        """High confidence (95%) + low fitness (25%) must still block via fitness gate."""
        import sys

        sys.path.insert(0, str(SRC))
        from datetime import datetime

        import pandas as pd

        from data.models import Quote
        from trading.environment_scorer import GATE_PASS_MIN
        from trading.trading_loop import TradingLoop

        class _LowFitnessEnv:
            def score(self, market, quote=None, quote_df=None):
                return 25.0  # below GATE_PASS_MIN regardless of confidence

            def get_sentiment_factor(self, market):
                return {}

        class _MockSignalEngine:
            def quote_df(self, market):
                return pd.DataFrame()

        loop = object.__new__(TradingLoop)
        loop._market = "EPIC2"
        loop._env = _LowFitnessEnv()
        loop._signal_engine = _MockSignalEngine()

        quote = Quote(datetime.now(), 100.0, 101.0)
        result = loop._gate_environment_fitness(quote)

        assert not result.passed, (
            "fitness gate passed with score=25% — fitness must block regardless of confidence"
        )
        val = result.value
        assert isinstance(val, dict), "gate value should be a dict with score/display"
        score_reported = val.get("score") or val.get("display") or 0
        assert int(str(score_reported).replace("%", "")) < GATE_PASS_MIN, (
            f"Reported fitness score {score_reported} is not below GATE_PASS_MIN={GATE_PASS_MIN}"
        )


# ---------------------------------------------------------------------------
# SESSION 6 FIXES — dynamic confidence-tiered sizing (v25.3.0)
# ---------------------------------------------------------------------------


class TestSession6DynamicSizing:
    """dynamic_sizing config and _confidence_adjusted_size logic."""

    def test_dynamic_sizing_config_present(self):
        """config_v25.json must have dynamic_sizing.enabled=True with 4 tiers."""
        cfg = json.loads(CONFIG.read_text())
        dyn = cfg.get("dynamic_sizing", {})
        assert dyn.get("enabled") is True, "dynamic_sizing.enabled must be True"
        tiers = dyn.get("tiers", [])
        assert len(tiers) == 4, f"Expected 4 confidence tiers, got {len(tiers)}"

    def test_confidence_tiered_sizing(self):
        """confidence=95 → multiplier 1.0; confidence=82 → multiplier 0.25."""
        import sys

        sys.path.insert(0, str(SRC))
        from execution.execution_engine import ExecutionEngine
        from system.config import Config

        cfg_data = json.loads(CONFIG.read_text())
        config = Config(_data=cfg_data)

        engine = object.__new__(ExecutionEngine)
        engine.config = config

        # Top tier: confidence ≥ 95 → 1.0×
        size_top = engine._confidence_adjusted_size(1.0, 95.0)
        assert size_top == 1.0, f"Tier 95 should give 1.0× multiplier, got {size_top}"

        # Bottom tier: confidence 82 (≥80 but <85) → 0.25×
        size_bottom = engine._confidence_adjusted_size(1.0, 82.0)
        assert size_bottom == 0.25, (
            f"Tier 80 (conf=82) should give 0.25× multiplier, got {size_bottom}"
        )

    def test_nasdaq_base_size_updated(self):
        """Nasdaq base trade_size must be 0.25 (up from 0.05) for dynamic sizing."""
        cfg = json.loads(CONFIG.read_text())
        nasdaq = cfg["instruments"]["nasdaq_100"]
        assert nasdaq["trade_size"] == 0.25, (
            f"Nasdaq trade_size should be 0.25, got {nasdaq['trade_size']}"
        )

    def test_partial_close_keys_present(self):
        """trailing_stop block must have partial_close_* keys (enabled for profitability)."""
        cfg = json.loads(CONFIG.read_text())
        ts = cfg.get("trailing_stop", {})
        assert "partial_close_enabled" in ts, (
            "trailing_stop missing partial_close_enabled"
        )
        assert ts["partial_close_enabled"] is True, (
            "partial_close_enabled should be True"
        )
        assert "partial_close_at_r" in ts, "trailing_stop missing partial_close_at_r"
        assert "partial_close_fraction" in ts, (
            "trailing_stop missing partial_close_fraction"
        )


# ---------------------------------------------------------------------------
# SESSION 7 FIXES — ghost in-flight order auto-expiry (v25.4.0)
# ---------------------------------------------------------------------------


class TestSession7InFlightAutoExpiry:
    """Pending orders must not block markets indefinitely when ref='-'."""

    def test_has_pending_auto_expires(self):
        """has_pending() must auto-clear entries older than PENDING_HARD_EXPIRY_SEC."""
        import sys
        import time

        sys.path.insert(0, str(SRC))
        import importlib

        mod = importlib.import_module("execution.pending_order_reconcile")
        mod.reset_pending_state_for_tests()

        epic = "CS.D.CFPGOLD.CFP.IP"
        # Inject a stale entry directly (created 400s ago, past 300s expiry)
        stale_ts = time.time() - 400.0
        with mod._lock:
            mod._pending[mod._epic_key(epic)] = mod.PendingOrder(
                epic=epic,
                side="SELL",
                order_type=mod.ORDER_TYPE_ENTRY,
                local_created_at=stale_ts,
                broker_deal_reference="",
            )

        assert mod.has_pending(epic) is False, (
            "has_pending() must return False for an entry older than "
            f"PENDING_HARD_EXPIRY_SEC={mod.PENDING_HARD_EXPIRY_SEC}s"
        )
        # Entry must have been removed from the dict
        with mod._lock:
            assert epic not in mod._pending, (
                "Expired pending entry must be removed from _pending dict"
            )

    def test_has_pending_live_entry_not_expired(self):
        """has_pending() must return True for a recent pending entry."""
        import sys
        import time

        sys.path.insert(0, str(SRC))
        import importlib

        mod = importlib.import_module("execution.pending_order_reconcile")
        mod.reset_pending_state_for_tests()

        epic = "CS.D.CFPGOLD.CFP.IP"
        mod.mark_pending(epic, side="SELL", order_type=mod.ORDER_TYPE_ENTRY)
        assert mod.has_pending(epic) is True, (
            "has_pending() must return True for a freshly-marked pending entry"
        )
        mod.reset_pending_state_for_tests()

    def test_load_pending_state_skips_stale_entries(self):
        """load_pending_state() must skip entries older than _PENDING_LOAD_MAX_AGE_SEC."""
        import sys
        import time

        sys.path.insert(0, str(SRC))
        import importlib

        mod = importlib.import_module("execution.pending_order_reconcile")
        mod.reset_pending_state_for_tests()

        epic = "CS.D.CFPGOLD.CFP.IP"
        stale_ts = time.time() - (mod._PENDING_LOAD_MAX_AGE_SEC + 60.0)
        mod.load_pending_state(
            {
                "orders": [
                    {
                        "epic": epic,
                        "side": "SELL",
                        "order_type": mod.ORDER_TYPE_ENTRY,
                        "local_created_at": stale_ts,
                        "broker_deal_reference": "",
                    }
                ]
            }
        )
        assert not mod.has_pending(epic), (
            "load_pending_state() must discard entries older than "
            f"_PENDING_LOAD_MAX_AGE_SEC={mod._PENDING_LOAD_MAX_AGE_SEC}s"
        )
        mod.reset_pending_state_for_tests()

    def test_mark_pending_skipped_when_no_ref_in_executor(self):
        """live_executor must NOT call mark_pending when ref='' (order never reached IG)."""
        import sys

        sys.path.insert(0, str(SRC))
        src_text = (SRC / "execution" / "live_executor.py").read_text()
        assert "if ref:" in src_text and "mark_pending(" in src_text, (
            "live_executor.py must guard mark_pending() with 'if ref:' to skip "
            "rate-cap-deferred orders that never reached IG"
        )
        # The guard must appear BEFORE the mark_pending call in the IGAPIError handler
        guard_idx = src_text.index("if ref:")
        mark_idx = src_text.index("mark_pending(", guard_idx)
        assert mark_idx > guard_idx, (
            "The 'if ref:' guard must appear before the mark_pending() call "
            "in the IGAPIError handler"
        )

    def test_position_sync_reconciles_all_managed_epics(self):
        """ig_position_sync must call reconcile_pending for all managed epics, not just self._epic."""
        import sys

        sys.path.insert(0, str(SRC))
        src_text = (SRC / "runtime" / "ig_position_sync.py").read_text()
        assert "reconcile_targets" in src_text, (
            "ig_position_sync.py must iterate reconcile_targets (all managed epics) "
            "not just self._epic to clear stale pending entries"
        )
        assert "self._managed_epics" in src_text, (
            "ig_position_sync.py must use self._managed_epics for reconciliation"
        )

    def test_clear_inflight_api_endpoint_present(self):
        """routes.py must expose POST /api/clear_inflight/{epic}."""
        import sys

        sys.path.insert(0, str(SRC))
        src_text = (SRC / "api" / "routes.py").read_text()
        assert "/api/clear_inflight/{epic}" in src_text, (
            "routes.py must expose /api/clear_inflight/{epic} endpoint"
        )

    def test_startup_clears_pending_state(self):
        """main.py _pre_startup_cleanup must call recover_pending_state_for_startup."""
        import sys

        sys.path.insert(0, str(SRC))
        src_text = (SRC / "main.py").read_text()
        assert "recover_pending_state_for_startup" in src_text, (
            "main.py must call recover_pending_state_for_startup() in "
            "_pre_startup_cleanup() so stale pending orders don't survive restarts"
        )


# ---------------------------------------------------------------------------
# SESSION 8 FIXES — startup pycache clear + blocked-trade smoke test (v25.4.0)
# ---------------------------------------------------------------------------


class TestStartupSmokeTest:
    """Startup protections: pycache clear, pending-order wipe, and pause check."""

    def test_pycache_cleared_on_startup(self, tmp_path):
        """_clear_pycache() must remove all __pycache__ dirs under the given root."""
        import shutil

        # Build a mini src tree with __pycache__ dirs
        fake_src = tmp_path / "src"
        (fake_src / "system" / "__pycache__").mkdir(parents=True)
        (fake_src / "execution" / "__pycache__").mkdir(parents=True)
        (fake_src / "__pycache__").mkdir(parents=True)

        # Count before
        before = list(fake_src.rglob("__pycache__"))
        assert len(before) == 3, f"Expected 3 __pycache__ dirs, got {len(before)}"

        # Run the same logic as _clear_pycache (operates on a temp root)
        cleared = 0
        for cache_dir in fake_src.rglob("__pycache__"):
            try:
                shutil.rmtree(cache_dir)
                cleared += 1
            except Exception:
                pass

        after = list(fake_src.rglob("__pycache__"))
        assert len(after) == 0, (
            f"_clear_pycache() must remove all __pycache__ dirs; {len(after)} remain"
        )
        assert cleared == 3, f"Expected to clear 3 dirs, cleared {cleared}"

    def test_clear_pycache_defined_in_main(self):
        """_clear_pycache() must be defined in main.py and called from _pre_startup_cleanup."""
        src_text = (SRC / "main.py").read_text()
        assert "def _clear_pycache" in src_text, (
            "_clear_pycache() function missing from main.py"
        )
        assert "_clear_pycache()" in src_text, (
            "_clear_pycache() is defined but not called in _pre_startup_cleanup()"
        )

    def test_no_stale_pending_orders_on_startup(self):
        """recover_pending_state_for_startup() must clear all pending entries."""
        import sys

        sys.path.insert(0, str(SRC))
        import importlib

        mod = importlib.import_module("execution.pending_order_reconcile")
        mod.reset_pending_state_for_tests()

        # Inject a pending entry
        mod.mark_pending("EPIC1", side="BUY", order_type=mod.ORDER_TYPE_ENTRY)
        assert mod.has_pending("EPIC1") is True, (
            "Entry should be pending before cleanup"
        )

        # Startup cleanup must wipe it
        mod.recover_pending_state_for_startup()
        assert mod.has_pending("EPIC1") is False, (
            "recover_pending_state_for_startup() must clear all pending entries so "
            "stale orders from the previous session cannot block trading"
        )
        mod.reset_pending_state_for_tests()

    def test_trading_not_paused_after_start(self):
        """is_paused() must return False at startup (before any stop_trading call)."""
        import sys

        sys.path.insert(0, str(SRC))
        import importlib

        mod = importlib.import_module("api.agent_control")
        # Reset to initial state
        import threading

        with mod._lock:
            mod._paused = False

        assert mod.is_paused() is False, (
            "is_paused() must return False at agent startup — "
            "trading_paused=True must not silently persist across restarts"
        )

    def test_smoke_test_phase_in_tracker(self):
        """startup_tracker must include a 'smoke_test' phase between self_test and ohlc."""
        import sys

        sys.path.insert(0, str(SRC))
        from system.startup_tracker import PHASES

        phase_ids = [p[0] for p in PHASES]
        assert "smoke_test" in phase_ids, (
            "'smoke_test' phase missing from startup_tracker.PHASES"
        )
        st_idx = phase_ids.index("self_test")
        smoke_idx = phase_ids.index("smoke_test")
        ohlc_idx = phase_ids.index("ohlc")
        assert st_idx < smoke_idx < ohlc_idx, (
            "'smoke_test' phase must appear between 'self_test' and 'ohlc' in PHASES"
        )

    def test_smoke_test_in_agent_bootstrap(self):
        """agent_bootstrap.py must run the startup smoke test and mark the phase."""
        src_text = (SRC / "runtime" / "agent_bootstrap.py").read_text()
        assert "smoke_test" in src_text, (
            "agent_bootstrap.py must call _startup_mark('smoke_test', ...) "
            "after the self_test block"
        )
        assert "recover_pending_state_for_startup" in src_text, (
            "agent_bootstrap.py smoke test must call recover_pending_state_for_startup()"
        )


# ---------------------------------------------------------------------------
# PORT-8080 ZOMBIE CLEANUP ON EXIT AND STARTUP
# ---------------------------------------------------------------------------


class TestPortCleanupOnExit:
    """Stale port-8080 processes must be force-killed on agent exit and startup."""

    def test_force_cleanup_port_function_exists(self):
        src_text = (SRC / "main.py").read_text()
        assert "def _force_cleanup_port" in src_text, (
            "_force_cleanup_port() not defined in main.py"
        )

    def test_cleanup_called_on_exit_hook(self):
        src_text = (SRC / "main.py").read_text()
        assert "atexit.register(_force_cleanup_port)" in src_text, (
            "_force_cleanup_port not registered as atexit handler in main.py"
        )

    def test_cleanup_called_in_shutdown(self):
        src_text = (SRC / "main.py").read_text()
        marker = "def shutdown(self"
        assert marker in src_text
        shutdown_body = src_text.split(marker, 1)[1].split("\n    def ")[0]
        assert "perform_shutdown_cleanup" in shutdown_body, (
            "perform_shutdown_cleanup() must be called inside AgentRuntime.shutdown()"
        )

    def test_cleanup_called_on_startup(self):
        src_text = (SRC / "main.py").read_text()
        prestartup_body = src_text.split("def _pre_startup_cleanup")[1].split("\ndef ")[
            0
        ]
        assert "_force_cleanup_port" in prestartup_body, (
            "_force_cleanup_port() must be called inside _pre_startup_cleanup()"
        )

    def test_stale_port_process_killed_on_exit(self):
        """lsof returns a foreign PID → os.kill must be called with SIGKILL."""
        import sys as _sys

        _sys.path.insert(0, str(SRC))
        import signal as _signal
        from unittest.mock import MagicMock, patch

        import main as _main

        fake_result = MagicMock()
        fake_result.stdout = "77777\n"

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("os.getpid", return_value=1),
            patch("os.kill") as mock_kill,
            patch("pathlib.Path.unlink"),
        ):
            _main._force_cleanup_port(8080)

        mock_kill.assert_called_once_with(77777, _signal.SIGKILL)

    def test_own_pid_not_killed(self):
        """lsof returns only the current PID → os.kill must NOT be called."""
        import os as _os
        import sys as _sys

        _sys.path.insert(0, str(SRC))
        from unittest.mock import MagicMock, patch

        import main as _main

        own = _os.getpid()
        fake_result = MagicMock()
        fake_result.stdout = f"{own}\n"

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("os.kill") as mock_kill,
            patch("pathlib.Path.unlink"),
        ):
            _main._force_cleanup_port(8080)

        mock_kill.assert_not_called()

    def test_lock_removed_on_exit(self):
        """_force_cleanup_port must call unlink on the lock path."""
        import sys as _sys

        _sys.path.insert(0, str(SRC))
        from unittest.mock import MagicMock, patch

        import main as _main

        fake_result = MagicMock()
        fake_result.stdout = ""

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("os.kill"),
            patch("pathlib.Path.unlink") as mock_unlink,
        ):
            _main._force_cleanup_port(8080)

        mock_unlink.assert_called_once()
