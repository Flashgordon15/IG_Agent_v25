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
    """london_morning must be in Wall Street and Nasdaq whitelists."""

    def test_wall_street_has_london_morning(self):
        cfg = json.loads(CONFIG.read_text())
        ws = cfg["instruments"]["wall_street"]
        assert "london_morning" in ws["trading_session_whitelist"], (
            "wall_street is missing london_morning in trading_session_whitelist"
        )

    def test_nasdaq_has_london_morning(self):
        cfg = json.loads(CONFIG.read_text())
        nas = cfg["instruments"]["nasdaq_100"]
        assert "london_morning" in nas["trading_session_whitelist"], (
            "nasdaq_100 is missing london_morning in trading_session_whitelist"
        )


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
    """Global rsi_buy_max must be 80 (raised from 78)."""

    def test_rsi_buy_max_is_80(self):
        cfg = json.loads(CONFIG.read_text())
        val = cfg.get("rsi_buy_max")
        assert val == 80, (
            f"rsi_buy_max={val}, expected 80. "
            "At 78, high-confidence signals (e.g. Nikkei 96%) are incorrectly filtered."
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
    """main.py must kill stale agent processes at startup."""

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
