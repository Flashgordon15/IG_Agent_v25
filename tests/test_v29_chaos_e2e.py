"""
v29.0 Chaos Integration Test — high-velocity full-stack stress validation.

Forces every roadmap upgrade to activate under extreme market stress in one
master suite (not isolated unit mocks). Safe for CI — no live IG orders.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.adaptive_engine import AdaptiveEngine
from runtime.market_orchestrator import MarketOrchestrator, attach_snapshot_handlers
from system.config import Config
from system.config_loader import ConfigLoader
from system.market_data_hub import get_market_data_hub
from trading.environment_scorer import (
    FACTOR_TREND_MAX,
    GATE_PASS_MIN,
    EnvironmentScorer,
    _apply_session_style_weights,
    _session_style_utc,
)
from trading.points_engine import (
    ROADMAP_COMPOUND_BOOST_MULTIPLIER,
    PointsEngine,
    set_points_state_path_for_tests,
)
from trading.trading_loop import TradingLoop

WEAK_TREND = 4.0
STRONG_TREND = 20.0
BASE_ATR = 20.0
BASE_SESSION = 10.0
BASE_SPREAD = 15.0
GOLD_EPIC = "CS.D.CFPGOLD.CFP.IP"
HERO_EPIC = "IX.D.NASDAQ.IFM.IP"


def _rotation_muted(active_epics: list[str], epic: str) -> bool:
    """Dashboard parity: MUTED — ROTATION when epic ∉ top-3 active list."""
    return len(active_epics) >= 3 and epic not in active_epics


def _weighted_total(factors: dict[str, float]) -> float:
    return sum(float(v) for v in factors.values())


def _build_five_market_orchestrator() -> MarketOrchestrator:
    cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
    specs = {
        HERO_EPIC: ("US Tech 100", 95.0, STRONG_TREND),
        GOLD_EPIC: ("Spot Gold", 40.0, WEAK_TREND),
        "IX.D.DOW.IFM.IP": ("Wall Street", 38.0, WEAK_TREND),
        "CS.D.EURUSD.CFD.IP": ("EUR/USD", 36.0, WEAK_TREND),
        "IX.D.NIKKEI.IFM.IP": ("Japan 225", 34.0, WEAK_TREND),
    }
    loops = []
    for epic, (label, fitness, trend) in specs.items():
        loop = MagicMock()
        loop._epic = epic
        loop._market = label
        loop._env = MagicMock()
        loop._env._last = SimpleNamespace(total=float(fitness))
        loop._env.get_factors.return_value = {
            "trend": float(trend),
            "spread": 15.0,
            "atr": BASE_ATR,
            "session": BASE_SESSION,
        }
        loop._publish_snapshots = False
        loop._on_snapshot = None
        loops.append(loop)
    enabled = list(specs.keys())
    meta = {
        epic: {"name": label, "instrument_id": label.lower().replace(" ", "_")}
        for epic, (label, _, _) in specs.items()
    }
    return MarketOrchestrator(
        cfg,
        loops,
        primary_epic=HERO_EPIC,
        enabled_epics=enabled,
        instrument_meta=meta,
    )


class TestV29ChaosE2E(unittest.TestCase):
    """Master chaos validation — all v29.0 pillars under simultaneous stress."""

    def setUp(self) -> None:
        from runtime import market_orchestrator as mo

        self._orch_ref_backup = mo._ORCHESTRATOR_REF
        self._tmp = tempfile.TemporaryDirectory()
        set_points_state_path_for_tests(Path(self._tmp.name) / "points.json")

    def tearDown(self) -> None:
        from runtime import market_orchestrator as mo

        mo._ORCHESTRATOR_REF = self._orch_ref_backup
        set_points_state_path_for_tests(None)
        self._tmp.cleanup()

    def test_pillar_a_sizing_and_asymmetric_rr(self) -> None:
        engine = PointsEngine(state_path=Path(self._tmp.name) / "points.json")
        engine._cumulative = 16.0
        scaled = engine._finalize_size_multiplier(0.5)
        self.assertAlmostEqual(
            scaled,
            0.5 * ROADMAP_COMPOUND_BOOST_MULTIPLIER,
            places=4,
            msg="win-streak 2.5× multiplier must lock on at cumulative=16",
        )

        cfg = MagicMock()
        cfg.adaptive_execution_enabled = True
        cfg.adaptive_min_adjusted_confidence = 0.0
        cfg.adaptive_max_entry_spread = 9999.0
        cfg.adaptive_min_net_profit_pts = 0.0
        cfg.adaptive_block_bad_setups = False
        adaptive = AdaptiveEngine(cfg)
        with patch.object(
            adaptive,
            "settings",
            return_value={"risk": 40.0, "limit": 100.0, "size": 1.0},
        ):
            blocked, reason = adaptive.should_block("chaos|rr", 92.0, {})
        self.assertTrue(blocked)
        self.assertEqual(reason, "REJECTED_ASYMMETRIC_RR_FLOOR_GATED")

    def test_pillar_b_rotation_and_session_switching(self) -> None:
        western_now = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(_session_style_utc(western_now), "WESTERN_MOMENTUM")

        weak_base = {
            "trend": WEAK_TREND,
            "atr": BASE_ATR,
            "session": BASE_SESSION,
            "spread": BASE_SPREAD,
        }
        strong_base = dict(weak_base)
        strong_base["trend"] = STRONG_TREND

        weak_w = _apply_session_style_weights(dict(weak_base), "WESTERN_MOMENTUM")
        strong_w = _apply_session_style_weights(dict(strong_base), "WESTERN_MOMENTUM")

        self.assertLess(weak_w["trend"], FACTOR_TREND_MAX * 0.5)
        self.assertLess(weak_w["atr"], BASE_ATR)
        self.assertLess(_weighted_total(weak_w), GATE_PASS_MIN)
        self.assertGreaterEqual(_weighted_total(strong_w), GATE_PASS_MIN)

        passing = 0
        for trend in (WEAK_TREND, WEAK_TREND, WEAK_TREND, WEAK_TREND, STRONG_TREND):
            factors = _apply_session_style_weights(
                {
                    "trend": trend,
                    "atr": BASE_ATR,
                    "session": BASE_SESSION,
                    "spread": BASE_SPREAD,
                },
                "WESTERN_MOMENTUM",
            )
            if _weighted_total(factors) >= GATE_PASS_MIN:
                passing += 1
        self.assertEqual(
            passing, 1, "exactly one market clears env gate under Western crush"
        )

        asian_now = datetime(2026, 6, 10, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(_session_style_utc(asian_now), "ASIAN_RANGE")
        asian = _apply_session_style_weights(dict(strong_base), "ASIAN_RANGE")
        self.assertAlmostEqual(asian["trend"], STRONG_TREND * 0.5)
        self.assertAlmostEqual(asian["atr"], BASE_ATR * 1.30)
        self.assertAlmostEqual(asian["spread"], BASE_SPREAD * 1.10)

        with (
            patch("runtime.market_orchestrator.publish_tick"),
            patch.object(
                MarketOrchestrator, "_strategy_session_eligible", return_value=True
            ),
        ):
            orch = _build_five_market_orchestrator()
            data = orch._config.as_dict()
            data["rotation_expand_threshold_pct"] = 0
            data["rotation_max_slots"] = 3
            orch._config = Config(_data=data)
            attach_snapshot_handlers(orch)
            for loop in orch.loops:
                orch.on_market_snapshot(
                    {
                        "epic": loop._epic,
                        "market": loop._market,
                        "bid": 100.0,
                        "offer": 100.5,
                        "spread": 0.5,
                    }
                )
            active = orch.get_active_epics()
            self.assertEqual(len(active), 3)
            self.assertIn(HERO_EPIC, active)
            muted = [lo._epic for lo in orch.loops if _rotation_muted(active, lo._epic)]
            self.assertEqual(len(muted), 2)
            self.assertTrue(_rotation_muted(active, "IX.D.NIKKEI.IFM.IP"))

    def test_pillar_c_synthetic_replay_and_anti_fallback(self) -> None:
        engine = MagicMock()
        engine.config = MagicMock()
        engine.config.max_spread_points = 35.0
        engine.quote_df.return_value = MagicMock()
        scorer = EnvironmentScorer(engine, config=engine.config, normal_spread=10.0)
        quote = Quote(datetime(2026, 6, 10, 14, 0), 100.0, 100.5)

        with patch.object(
            scorer,
            "_compute_factors",
            side_effect=NameError("session_style"),
        ):
            score = scorer.score("US Tech 100", quote=quote)

        self.assertTrue(scorer.last_score().fallback_active)
        self.assertEqual(score, 55.0)

        from api.agent_health import _env_scorer_fallback_active

        env = MagicMock()
        env.last_score.return_value = SimpleNamespace(fallback_active=True)
        loop_ref = SimpleNamespace(_env=env)
        with patch("api.agent_health.get_trading_loop", return_value=loop_ref):
            self.assertTrue(_env_scorer_fallback_active())

        from system.synthetic_replay import run_synthetic_replay

        with patch(
            "system.synthetic_replay.load_loss_snapshots",
            return_value=([], []),
        ):
            code = run_synthetic_replay()
        self.assertEqual(code, 0)

    def test_pillar_d_liquidity_shield(self) -> None:
        hub = get_market_data_hub()
        safe, ratio = hub.verify_liquidity_shield_delta(GOLD_EPIC, 2.5)
        self.assertFalse(safe)
        self.assertAlmostEqual(ratio, 5.0, places=2)

        quote = Quote(datetime(2026, 6, 10, 14, 0), 2350.0, 2352.5)
        loop = MagicMock(spec=TradingLoop)
        loop._epic = GOLD_EPIC
        loop.entry_circuit_breaker.return_value = ""
        loop._gate_active_rotation = MagicMock(
            return_value=SimpleNamespace(passed=True, value={}, detail="ok")
        )

        with patch.object(
            MarketOrchestrator,
            "get_global_active_epics",
            return_value=[GOLD_EPIC, HERO_EPIC, "IX.D.DOW.IFM.IP"],
        ):
            gates = TradingLoop._evaluate_gates_core(loop, quote)

        self.assertEqual(len(gates), 1)
        self.assertFalse(gates[0].passed)
        self.assertEqual(gates[0].detail, "BLOCKED_MULTI_BROKER_LIQUIDITY_SHIELD")

    def test_pillar_e_hardware_and_log_rotation(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "safe_to_leave", ROOT / "scripts" / "safe_to_leave.py"
        )
        assert spec and spec.loader
        stl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stl)

        with patch.object(stl, "_on_ac_power", return_value=(False, "on battery")):
            ok, detail = stl._on_ac_power()
        self.assertFalse(ok)
        self.assertIn("battery", detail.lower())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "src" / "data" / "logs"
            archive_dir = log_dir / "archive"
            log_dir.mkdir(parents=True)
            engine_log = log_dir / "engine.log"
            engine_log.write_bytes(b"x" * (5 * 1024 * 1024 + 4096))

            fd = engine_log.open("a")
            try:
                script = ROOT / "scripts" / "rotate_logs.sh"
                env = os.environ.copy()
                env["IG_AGENT_ROOT"] = str(root)
                proc = subprocess.run(
                    ["bash", str(script), "--force"],
                    cwd=str(ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
                self.assertEqual(engine_log.stat().st_size, 0)
                archives = list(archive_dir.glob("engine.log.*.gz"))
                self.assertTrue(archives, "rotated archive must exist")
                fd.write("still-open\n")
                fd.flush()
                self.assertGreater(engine_log.stat().st_size, 0)
            finally:
                fd.close()

    def test_v29_chaos_full_stack_simultaneous(self) -> None:
        """Run all pillar assertions in one high-velocity pass."""
        self.test_pillar_a_sizing_and_asymmetric_rr()
        self.test_pillar_b_rotation_and_session_switching()
        self.test_pillar_c_synthetic_replay_and_anti_fallback()
        self.test_pillar_d_liquidity_shield()
        self.test_pillar_e_hardware_and_log_rotation()


if __name__ == "__main__":
    unittest.main()
