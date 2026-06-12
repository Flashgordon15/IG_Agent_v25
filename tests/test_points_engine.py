"""Tests for points_engine — scoring, states, session rules, persistence."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from trading.points_engine import (  # noqa: E402
    EQUITY_LOCK_SESSION_MILESTONE,
    EQUITY_LOCK_SIGNAL_THRESHOLD,
    HEALTHY_CUMULATIVE_MIN,
    PointsEngine,
    next_tier_preview,
    set_points_state_path_for_tests,
)


def _make_store() -> tuple[LearningStore, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    db = Path(tmp.name) / "learning.db"
    store = LearningStore(str(db))
    store.connect()
    return store, tmp


def _insert_confirmed(
    store: LearningStore,
    *,
    result: str,
    pnl: float,
    confidence: float = 90.0,
) -> None:
    store.conn.execute(
        """
        INSERT INTO trades(
            opened_at, closed_at, market, epic, side, entry, exit, size,
            stop, target, pnl_points, result, confidence, adjusted_confidence,
            setup_key, dry_run, deal_reference, notes, ig_pnl_currency, source
        ) VALUES (
            '2026-01-01 00:00:00', '2026-01-01 01:00:00', 'Japan 225',
            'IX.D.NIKKEI.IFM.IP', 'BUY', 100, 110, 1,
            90, 120, ?, ?, ?, ?, 'BUY|bull|asia_early', 0, 'DIAAA1', '', ?, 'strategy'
        )
        """,
        (pnl, result, confidence, confidence, pnl),
    )
    store.conn.commit()


class PointsEngineScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "points_state.json"
        set_points_state_path_for_tests(self.state_path)
        self.store, self.store_tmp = _make_store()
        self.engine = PointsEngine(self.store, state_path=self.state_path)

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self.store.close()
        self.store_tmp.cleanup()
        self.tmp.cleanup()

    def test_flat_scoring_when_fewer_than_five_confirmed(self) -> None:
        for _ in range(3):
            _insert_confirmed(self.store, result="WIN", pnl=20.0)
        score = self.engine.record_trade("WIN", 95.0, 20.0)
        self.assertAlmostEqual(score, 1.0)
        score_loss = self.engine.record_trade("LOSS", 95.0, -15.0)
        self.assertAlmostEqual(score_loss, -1.0)
        self.assertAlmostEqual(self.engine.record_trade("BREAKEVEN", 90.0, 0.0), 0.0)

    def test_scaled_scoring_when_five_or_more_confirmed(self) -> None:
        for _ in range(5):
            _insert_confirmed(self.store, result="WIN", pnl=10.0)
        engine = PointsEngine(self.store, state_path=self.state_path)
        score = engine.record_trade("WIN", 95.0, 10.0)
        self.assertGreater(score, 1.0)
        self.assertAlmostEqual(score, 3.0, places=3)

    def test_loss_high_conviction_scaled(self) -> None:
        for _ in range(5):
            _insert_confirmed(self.store, result="LOSS", pnl=-10.0)
        engine = PointsEngine(self.store, state_path=self.state_path)
        score = engine.record_trade("LOSS", 93.0, -10.0)
        self.assertLess(score, -1.0)
        self.assertAlmostEqual(score, -4.0, places=3)


class PointsEngineStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "points_state.json"
        set_points_state_path_for_tests(self.state_path)
        self.engine = PointsEngine(state_path=self.state_path)

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def test_state_from_cumulative_bands(self) -> None:
        self.engine._cumulative = 12.0
        self.assertEqual(self.engine.get_state(), "HEALTHY")
        self.engine._cumulative = 0.0
        self.assertEqual(self.engine.get_state(), "CAUTION")
        self.engine._cumulative = -10.0
        self.assertEqual(self.engine.get_state(), "WARNING")
        self.engine._stop_latched = True
        self.engine._cumulative = 20.0
        self.assertEqual(self.engine.get_state(), "STOP")

    def test_recovery_three_wins_promotes_to_caution(self) -> None:
        self.engine._cumulative = -10.0
        self.engine._recovery_wins = 3
        self.engine._stop_latched = False
        self.assertEqual(self.engine.get_state(), "CAUTION")

    def test_recovery_five_wins_promotes_to_healthy(self) -> None:
        self.engine._cumulative = 5.0
        self.engine._recovery_wins = 5
        self.assertEqual(self.engine.get_state(), "HEALTHY")

    def test_get_state_safe_default_on_error(self) -> None:
        with patch.object(
            PointsEngine, "_effective_state_unlocked", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(self.engine.get_state(), "HEALTHY")


class PointsEngineThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        set_points_state_path_for_tests(Path(self.tmp.name) / "points.json")
        self.engine = PointsEngine(state_path=Path(self.tmp.name) / "points.json")

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def test_trade_confidence_threshold_is_max_of_points_and_config(self) -> None:
        # cfg.confidence_floor=80, signal_threshold=85 → max(80, 85) = 85
        cfg = type(
            "Cfg",
            (),
            {
                "signal_threshold": 85.0,
                "confidence_floor": 80.0,
                "confidence_floor_recovery_per_win": 1.0,
            },
        )()
        with patch.object(self.engine, "get_state", return_value="HEALTHY"):
            self.assertEqual(self.engine.trade_confidence_threshold(cfg), 85.0)
        # WARNING state always returns CONF_HIGH (92) regardless of config floor
        with patch.object(self.engine, "get_state", return_value="WARNING"):
            self.assertEqual(self.engine.trade_confidence_threshold(cfg), 92.0)
        # cfg.confidence_floor=75 (bootstrap mode), signal_threshold=75 → 75
        cfg2 = type(
            "Cfg",
            (),
            {
                "signal_threshold": 75.0,
                "confidence_floor": 75.0,
                "confidence_floor_recovery_per_win": 1.0,
            },
        )()
        with patch.object(self.engine, "get_state", return_value="CAUTION"):
            self.assertEqual(self.engine.trade_confidence_threshold(cfg2), 75.0)

    def test_min_size_confidence_threshold_caution_is_55(self) -> None:
        # CAUTION now gives 0.5× for all conf >= CONF_MARGINAL_MIN (55), so threshold is 55
        with patch.object(self.engine, "get_state", return_value="CAUTION"):
            self.assertEqual(self.engine.min_size_confidence_threshold(), 55.0)


class PointsEngineSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        set_points_state_path_for_tests(Path(self.tmp.name) / "points.json")
        self.engine = PointsEngine(state_path=Path(self.tmp.name) / "points.json")

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def test_session_pause_after_consecutive_losses(self) -> None:
        # SESSION_LOSS_STREAK_TRIGGER=6, SIGNALS_TO_SKIP_AFTER_STREAK=1
        for _ in range(6):
            self.engine.record_trade("LOSS", 90.0, -5.0)
        self.assertTrue(self.engine.is_session_paused())
        self.assertTrue(self.engine.consume_signal_skip())
        self.assertFalse(self.engine.is_session_paused())

    def test_day_stop_always_false_when_disabled(self) -> None:
        # Day-stop is disabled — max_daily_loss_gbp gate is the hard stop instead.
        for _ in range(10):
            self.engine.record_trade("LOSS", 90.0, -1.0)
        self.assertFalse(self.engine.is_day_stopped())

    def test_reset_session_clears_day_stop_and_pause(self) -> None:
        for _ in range(3):
            self.engine.record_trade("LOSS", 90.0, -2.0)
        self.engine.reset_session()
        self.assertFalse(self.engine.is_day_stopped())
        self.assertFalse(self.engine.is_session_paused())


class PointsEngineThresholdSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        set_points_state_path_for_tests(Path(self.tmp.name) / "points.json")
        self.engine = PointsEngine(state_path=Path(self.tmp.name) / "points.json")

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def _set_state(self, cumulative: float, *, stop: bool = False) -> None:
        self.engine._cumulative = cumulative
        self.engine._stop_latched = stop
        self.engine._recovery_wins = 0

    def test_threshold_by_state(self) -> None:
        self._set_state(15.0)
        self.assertEqual(self.engine.get_threshold(), 55.0)
        self._set_state(0.0)
        self.assertEqual(self.engine.get_threshold(), 55.0)
        self._set_state(-10.0)
        self.assertEqual(self.engine.get_threshold(), 92.0)
        self._set_state(-20.0, stop=True)
        self.assertEqual(self.engine.get_threshold(), 100.0)

    def test_size_multiplier_healthy_bands(self) -> None:
        # Progressive multiplier: cum=15 → tier_mult=1.5 (HEALTHY: cum > 4)
        # Roadmap ×2.5 when cumulative >= 15 (hysteresis holds through 10–14.9)
        self._set_state(15.0)
        self.assertEqual(self.engine.get_size_multiplier(93.0), 3.75)
        self.assertEqual(self.engine.get_size_multiplier(88.0), 1.875)
        # Core standard: stacked decay floored at CORE_MULTIPLIER_FLOOR (0.8×)
        self.assertAlmostEqual(self.engine.get_size_multiplier(82.0), 0.8)
        # Probe band floored at PROBE_MULTIPLIER_FLOOR (0.5×) when raw×roadmap is lower
        mult_75 = self.engine.get_size_multiplier(75.0)
        self.assertGreaterEqual(mult_75, 0.5)

    def test_size_multiplier_caution_bands(self) -> None:
        self._set_state(0.0)
        self.assertEqual(self.engine.get_size_multiplier(89.0), 0.8)
        self.assertEqual(self.engine.get_size_multiplier(82.0), 0.8)
        self.assertEqual(self.engine.get_size_multiplier(79.0), 0.5)

    def test_size_multiplier_spec_matrix(self) -> None:
        # CAUTION: core/full signals floored at 0.8×; probe at 0.5×
        self._set_state(0.0)
        self.assertEqual(self.engine.get_size_multiplier(82.0), 0.8)
        self.assertEqual(self.engine.get_size_multiplier(89.0), 0.8)
        # HEALTHY (cum=15.0): tier_mult=1.5 × roadmap 2.5 → standard=1.875, high=3.75
        self._set_state(15.0)
        self.assertEqual(self.engine.get_size_multiplier(86.0), 1.875)
        self.assertEqual(self.engine.get_size_multiplier(93.0), 3.75)
        # STOP: always 0
        self._set_state(-20.0, stop=True)
        self.assertEqual(self.engine.get_size_multiplier(99.0), 0.0)

    def test_roadmap_compound_hysteresis_band(self) -> None:
        self._set_state(16.0)
        self.assertAlmostEqual(self.engine._roadmap_cumulative_scale(), 2.5)
        self.engine._cumulative = 12.0
        self.assertAlmostEqual(self.engine._roadmap_cumulative_scale(), 2.5)
        self.engine._cumulative = 9.0
        self.assertAlmostEqual(self.engine._roadmap_cumulative_scale(), 1.0)
        self.engine._cumulative = 12.0
        self.assertAlmostEqual(self.engine._roadmap_cumulative_scale(), 1.0)

    def test_size_multiplier_warning_only_high(self) -> None:
        self._set_state(-10.0)
        self.assertEqual(self.engine.get_size_multiplier(93.0), 0.25)
        self.assertEqual(self.engine.get_size_multiplier(88.0), 0.0)

    def test_equity_lock_halves_size_and_raises_threshold(self) -> None:
        self._set_state(15.0)
        base = self.engine.get_size_multiplier(93.0)
        self.engine._session_score = EQUITY_LOCK_SESSION_MILESTONE
        self.assertTrue(self.engine.equity_lock_active())
        self.assertEqual(
            self.engine.protected_signal_threshold_floor(),
            EQUITY_LOCK_SIGNAL_THRESHOLD,
        )
        locked = self.engine.get_size_multiplier(93.0)
        self.assertAlmostEqual(locked, base * 0.5)

        class _Cfg:
            signal_threshold = 55.0
            confidence_floor = 55.0
            confidence_floor_recovery_per_win = 1.0

        self.assertEqual(
            self.engine.trade_confidence_threshold(_Cfg()),
            EQUITY_LOCK_SIGNAL_THRESHOLD,
        )

    def test_equity_lock_resets_on_session_reset(self) -> None:
        self.engine._session_score = EQUITY_LOCK_SESSION_MILESTONE
        self.engine._equity_lock_announced = True
        self.engine.reset_session()
        self.assertFalse(self.engine.equity_lock_active())
        self.assertFalse(self.engine._equity_lock_announced)


class PointsEnginePersistenceTests(unittest.TestCase):
    def test_persistence_round_trip(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state_path = Path(tmp.name) / "state" / "points_state.json"
        set_points_state_path_for_tests(state_path)

        e1 = PointsEngine(state_path=state_path)
        e1.record_trade("WIN", 92.0, 10.0)
        e1.record_trade("WIN", 88.0, 8.0)
        e1._signals_to_skip = 2
        e1._day_stopped = False
        e1._cumulative = 5.5
        e1._persist()

        e2 = PointsEngine(state_path=state_path)
        self.assertAlmostEqual(e2._cumulative, 5.5)
        self.assertEqual(e2._signals_to_skip, 2)
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)

        set_points_state_path_for_tests(None)
        tmp.cleanup()


class PointsMilestoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "points.json"
        set_points_state_path_for_tests(self.state_path)
        self.engine = PointsEngine(state_path=self.state_path)

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def test_record_milestone_adds_bonus_once_configured(self) -> None:
        score = self.engine.record_milestone("breakeven", market="EUR/USD", trade_id=1)
        self.assertAlmostEqual(score, 0.5)
        snap = self.engine.snapshot()
        self.assertAlmostEqual(snap.cumulative, 0.5)
        self.assertAlmostEqual(snap.last_trade_score, 0.5)

    def test_record_milestone_unknown_kind_is_zero(self) -> None:
        self.assertEqual(self.engine.record_milestone("unknown"), 0.0)

    def test_next_tier_preview_from_caution(self) -> None:
        preview = next_tier_preview(0.0, "CAUTION")
        self.assertEqual(preview["kind"], "state")
        self.assertAlmostEqual(preview["points_to_next"], HEALTHY_CUMULATIVE_MIN + 0.01)

    def test_next_tier_preview_compound_boost(self) -> None:
        preview = next_tier_preview(12.0, "HEALTHY")
        self.assertEqual(preview["label"], "Compound boost (2.5× stack)")
        self.assertAlmostEqual(preview["points_to_next"], 3.0)

    def test_get_next_tier_wrapper(self) -> None:
        self.engine._cumulative = 12.0
        tier = self.engine.get_next_tier()
        self.assertEqual(tier["state"], "HEALTHY")
        self.assertAlmostEqual(tier["points_to_next"], 3.0)


if __name__ == "__main__":
    unittest.main()
