"""Tests for the offline dual-engine (sniper|long) ML learning plane.

Covers: style/epic resolution + write-path stamping, shadow replay-outcome
isolation for both styles, and the trainer improvement-epoch gate.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import data.ml_training_store as mls
from data.ml_training_store import MLTrainingStore
from data.shadow_training_registry import (
    count_replay_rows,
    ensure_replay_schema,
    replay_style_epic_counts,
    upsert_shadow_replay,
)
from diagnostics.ml_strategy_review import improvement_epoch_eligible_for_verdict
from ml.style_epic import LONG, SNIPER, UNKNOWN, epic_for_instrument, resolve_ml_style


class StyleEpicResolveTests(unittest.TestCase):
    def test_epic_for_instrument_display_names(self) -> None:
        self.assertEqual(epic_for_instrument("Wall Street"), "IX.D.DOW.IFM.IP")
        self.assertEqual(epic_for_instrument("Japan 225"), "IX.D.NIKKEI.IFM.IP")
        self.assertEqual(epic_for_instrument("Spot Gold"), "CS.D.CFPGOLD.CFP.IP")
        self.assertEqual(epic_for_instrument("EUR/USD"), "CS.D.EURUSD.CFD.IP")
        self.assertEqual(epic_for_instrument("Germany 40"), "IX.D.DAX.IFM.IP")

    def test_epic_prefers_valid_fallback_epic(self) -> None:
        self.assertEqual(
            epic_for_instrument("Whatever", fallback_epic="IX.D.DOW.IFM.IP"),
            "IX.D.DOW.IFM.IP",
        )

    def test_epic_unknown_is_blank_not_fabricated(self) -> None:
        self.assertEqual(epic_for_instrument("Totally Unknown Market"), "")

    def test_style_explicit_and_origin(self) -> None:
        self.assertEqual(resolve_ml_style(style_hint="scalp"), SNIPER)
        self.assertEqual(resolve_ml_style(style_hint="macro"), LONG)
        self.assertEqual(resolve_ml_style(engine_origin="QUANT_SNIPER"), SNIPER)
        self.assertEqual(resolve_ml_style(engine_origin="MACRO_SENTINEL"), LONG)

    def test_style_hold_split_matches_journal_convention(self) -> None:
        self.assertEqual(resolve_ml_style(hold_sec=30.0), SNIPER)
        self.assertEqual(resolve_ml_style(hold_sec=600.0), LONG)

    def test_style_unknown_when_undetermined(self) -> None:
        self.assertEqual(resolve_ml_style(), UNKNOWN)
        self.assertEqual(resolve_ml_style(style_hint="supervised_exit"), UNKNOWN)


class WritePathStampingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ml_training_store.jsonl"
        mls.reset_ml_training_store_for_tests()
        mls.set_store_path_for_tests(self.path)
        self.store = MLTrainingStore(self.path)

    def tearDown(self) -> None:
        mls.reset_ml_training_store_for_tests()
        self.tmp.cleanup()

    def test_normalize_stamps_style_epic_and_score(self) -> None:
        import json

        self.store.record_entry(
            "DEAL-SNIPE",
            {
                "instrument": "Wall Street",
                "setup_name": "BUY|bull|us_afternoon",
                "source": "agent",
                "ml_score_at_entry": 0.71,
            },
        )
        self.store.record_exit(
            "DEAL-SNIPE",
            {
                "result": "WIN",
                "gbp_pnl": 12.0,
                "ig_pnl_currency": 12.0,
                "hold_sec": 45.0,
                "engine_origin": "QUANT_SNIPER",
                "confirmed": True,
                "source": "agent",
            },
        )
        row = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(row["epic"], "IX.D.DOW.IFM.IP")
        self.assertEqual(row["style"], SNIPER)
        self.assertEqual(row["ml_score_at_entry"], 0.71)
        self.assertEqual(row["hold_sec"], 45.0)

    def test_long_hold_stamps_long_style(self) -> None:
        import json

        self.store.record_entry(
            "DEAL-LONG",
            {"instrument": "Japan 225", "setup_name": "x", "source": "agent"},
        )
        self.store.record_exit(
            "DEAL-LONG",
            {
                "result": "WIN",
                "ig_pnl_currency": 30.0,
                "hold_sec": 900.0,
                "confirmed": True,
                "source": "agent",
            },
        )
        row = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(row["style"], LONG)
        self.assertEqual(row["epic"], "IX.D.NIKKEI.IFM.IP")


class ShadowReplayIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_replay_schema(self.conn.cursor())
        # A pre-existing curated shadow_training_registry table must stay untouched.
        from data.shadow_training_registry import ensure_schema

        ensure_schema(self.conn.cursor())
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _row(self, style: str, ref: str, result: str, score: float) -> dict:
        return {
            "replay_ref": ref,
            "source_ref": "ts",
            "style": style,
            "engine": "cfd_micro_sniper" if style == SNIPER else "sb_macro_ltr",
            "epic": "CS.D.CFPGOLD.CFP.IP",
            "market": "Spot Gold",
            "side": "BUY",
            "result": result,
            "ml_score_at_entry": score,
            "hold_sec": 90.0 if style == SNIPER else 1800.0,
            "horizon_bars": 3 if style == SNIPER else 6,
            "setup_key": "BUY|x",
            "session_window": "asia_early",
        }

    def test_both_styles_written_and_isolated(self) -> None:
        upsert_shadow_replay(self.conn, self._row(SNIPER, "RPLY-sniper-1", "WIN", 0.6))
        upsert_shadow_replay(self.conn, self._row(SNIPER, "RPLY-sniper-2", "LOSS", 0.4))
        upsert_shadow_replay(self.conn, self._row(LONG, "RPLY-long-1", "WIN", 0.55))

        self.assertEqual(count_replay_rows(self.conn), 3)
        self.assertEqual(count_replay_rows(self.conn, style=SNIPER), 2)
        self.assertEqual(count_replay_rows(self.conn, style=LONG), 1)

        # Every replay row is shadow-isolated.
        rows = self.conn.execute(
            "SELECT is_shadow FROM shadow_replay_outcomes"
        ).fetchall()
        self.assertTrue(all(int(r["is_shadow"]) == 1 for r in rows))

        # The curated IG-import registry is NOT polluted by replay writes.
        n_reg = self.conn.execute(
            "SELECT COUNT(*) FROM shadow_training_registry"
        ).fetchone()[0]
        self.assertEqual(n_reg, 0)

        counts = replay_style_epic_counts(self.conn)
        self.assertEqual(counts[SNIPER]["wins"], 1)
        self.assertEqual(counts[SNIPER]["losses"], 1)
        self.assertEqual(counts[LONG]["wins"], 1)

    def test_upsert_is_idempotent_on_ref(self) -> None:
        upsert_shadow_replay(self.conn, self._row(SNIPER, "RPLY-sniper-1", "WIN", 0.6))
        upsert_shadow_replay(self.conn, self._row(SNIPER, "RPLY-sniper-1", "LOSS", 0.4))
        self.assertEqual(count_replay_rows(self.conn, style=SNIPER), 1)
        row = self.conn.execute(
            "SELECT result FROM shadow_replay_outcomes WHERE replay_ref=?",
            ("RPLY-sniper-1",),
        ).fetchone()
        self.assertEqual(str(row["result"]).upper(), "LOSS")


class TrainerGateTests(unittest.TestCase):
    def test_improvement_epoch_blocked_under_not_measurable(self) -> None:
        self.assertFalse(improvement_epoch_eligible_for_verdict("NOT_MEASURABLE"))
        self.assertFalse(improvement_epoch_eligible_for_verdict("APP_BLOCKED"))
        self.assertFalse(improvement_epoch_eligible_for_verdict(""))
        self.assertFalse(improvement_epoch_eligible_for_verdict(None))

    def test_improvement_epoch_allowed_when_edge_measured(self) -> None:
        self.assertTrue(improvement_epoch_eligible_for_verdict("EDGE_OK"))
        self.assertTrue(improvement_epoch_eligible_for_verdict("EDGE_WEAK"))
        self.assertTrue(improvement_epoch_eligible_for_verdict("NO_EDGE"))

    def test_harness_train_never_claims_epoch(self) -> None:
        # The harness must hard-code improvement_epoch_claimed False regardless of
        # verdict — offline learning never annotates a live improvement epoch.
        src = (ROOT / "scripts" / "ml_replay_learn.py").read_text(encoding="utf-8")
        self.assertIn('"improvement_epoch_claimed": False', src)
        self.assertIn("improvement_epoch=False", src)


class WidenedReplayFeatureTests(unittest.TestCase):
    def test_feature_names_cover_microstructure_set(self) -> None:
        from ml.replay_features import FEATURE_NAMES

        self.assertGreaterEqual(len(FEATURE_NAMES), 10)
        for name in (
            "rsi",
            "atr_ratio",
            "spread_ratio",
            "range_ratio",
            "ret_1",
            "ret_3",
            "ret_6",
            "ret_12",
            "momentum_12",
            "vol_regime_idx",
            "session_window_idx",
        ):
            self.assertIn(name, FEATURE_NAMES)

    def test_features_from_ohlc_no_lookahead_and_real_returns(self) -> None:
        from ml.replay_features import FEATURE_NAMES, features_from_ohlc

        bars = []
        px = 100.0
        for i in range(30):
            px += 1.0 if i % 2 == 0 else -0.5
            bars.append(
                {
                    "t": f"2026-04-01T{i:02d}:00:00",
                    "o": px,
                    "h": px + 1.0,
                    "l": px - 1.0,
                    "c": px,
                    "spread": 2.0,
                }
            )
        feats = features_from_ohlc(
            bars,
            20,
            stop_pts=10.0,
            rsi=55.0,
            atr=2.0,
            spread=2.0,
            adjusted_score=60.0,
            raw_score=58.0,
            session_window="london_morning",
            vol_regime="normal",
        )
        self.assertEqual(set(feats), set(FEATURE_NAMES))
        self.assertAlmostEqual(feats["spread_ratio"], 0.2, places=5)
        self.assertAlmostEqual(feats["atr_ratio"], 0.2, places=5)
        self.assertEqual(feats["session_window_idx"], 1.0)
        self.assertEqual(feats["vol_regime_idx"], 1.0)
        # ret_1 uses only closes at/before idx 20
        self.assertNotEqual(feats["ret_1"], 0.0)

    def test_forward_label_horizons_match_sniper_and_long(self) -> None:
        """Sniper=3-bar / long=6-bar stop-touch labels — no fabricated outcomes."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "replay_signals_test", ROOT / "scripts" / "replay_signals.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Rising path: BUY wins 3-bar and 6-bar when high clears entry+stop.
        bars = [
            {"t": "t0", "o": 100, "h": 100, "l": 100, "c": 100, "spread": 1},
            {"t": "t1", "o": 100, "h": 105, "l": 99, "c": 104, "spread": 1},
            {"t": "t2", "o": 104, "h": 106, "l": 103, "c": 105, "spread": 1},
            {"t": "t3", "o": 105, "h": 112, "l": 104, "c": 110, "spread": 1},
            {"t": "t4", "o": 110, "h": 111, "l": 109, "c": 110, "spread": 1},
            {"t": "t5", "o": 110, "h": 111, "l": 109, "c": 110, "spread": 1},
            {"t": "t6", "o": 110, "h": 111, "l": 109, "c": 110, "spread": 1},
        ]
        entry = 100.0
        stop = 5.0
        fh3, fl3, _ = mod._forward_extremes(bars, 0, 3)
        fh6, fl6, _ = mod._forward_extremes(bars, 0, 6)
        lab3 = mod._label_direction("BUY", entry, fwd_high=fh3, fwd_low=fl3, stop_pts=stop)
        lab6 = mod._label_direction("BUY", entry, fwd_high=fh6, fwd_low=fl6, stop_pts=stop)
        self.assertEqual(lab3, "WIN")
        self.assertEqual(lab6, "WIN")

        # Adverse spike within 3 bars → LOSS for BUY.
        bars_bad = [
            {"t": "t0", "o": 100, "h": 100, "l": 100, "c": 100, "spread": 1},
            {"t": "t1", "o": 100, "h": 101, "l": 90, "c": 95, "spread": 1},
            {"t": "t2", "o": 95, "h": 96, "l": 94, "c": 95, "spread": 1},
            {"t": "t3", "o": 95, "h": 96, "l": 94, "c": 95, "spread": 1},
        ]
        fh3b, fl3b, _ = mod._forward_extremes(bars_bad, 0, 3)
        self.assertEqual(
            mod._label_direction(
                "BUY", entry, fwd_high=fh3b, fwd_low=fl3b, stop_pts=stop
            ),
            "LOSS",
        )

    def test_multi_epic_build_targets_include_dow_and_nikkei(self) -> None:
        src = (ROOT / "scripts" / "ml_replay_learn.py").read_text(encoding="utf-8")
        self.assertIn("IX.D.DOW.IFM.IP", src)
        self.assertIn("IX.D.NIKKEI.IFM.IP", src)
        self.assertIn("CS.D.CFPGOLD.CFP.IP", src)
        self.assertIn("build_multi_epic_replay", src)


if __name__ == "__main__":
    unittest.main()
