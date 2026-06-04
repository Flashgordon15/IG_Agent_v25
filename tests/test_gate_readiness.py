"""Tests for aggregate trade readiness across the 7 gates."""

from __future__ import annotations

import unittest

from trading.environment_scorer import GATE_PASS_MIN
from trading.gate_readiness import (
    compute_trade_readiness,
    format_health_badge_text,
    gate_contribution,
)
from trading.trading_loop import GateResult


class TestGateContribution(unittest.TestCase):
    def test_passed_gate_counts_full(self) -> None:
        g = GateResult(name="session_open", passed=True, value=True)
        self.assertEqual(gate_contribution(g), 1.0)

    def test_failed_binary_gate_is_zero(self) -> None:
        g = GateResult(name="points_state", passed=False, value="STOP")
        self.assertEqual(gate_contribution(g), 0.0)

    def test_environment_fitness_partial(self) -> None:
        g = GateResult(
            name="environment_fitness",
            passed=False,
            value={"score": 30},
        )
        self.assertAlmostEqual(gate_contribution(g, fitness_min=40.0), 0.75)

    def test_signal_confidence_partial(self) -> None:
        g = GateResult(
            name="signal_confidence",
            passed=False,
            value={"confidence": 56.0, "threshold": 80.0},
        )
        self.assertAlmostEqual(gate_contribution(g), 0.7)

    def test_cold_start_bars_partial(self) -> None:
        g = GateResult(
            name="cold_start_gap",
            passed=False,
            value={"cold": True, "gap": False, "bars": 3},
        )
        self.assertAlmostEqual(gate_contribution(g), 0.5)

    def test_cold_start_gap_blocks(self) -> None:
        g = GateResult(
            name="cold_start_gap",
            passed=False,
            value={"cold": False, "gap": True, "bars": 6},
        )
        self.assertEqual(gate_contribution(g), 0.0)


class TestComputeTradeReadiness(unittest.TestCase):
    def _seven_gates(self, **overrides: GateResult) -> list[GateResult]:
        names = [
            "session_open",
            "cold_start_gap",
            "environment_fitness",
            "points_state",
            "risk_validation",
            "signal_confidence",
            "execution",
        ]
        gates = [
            GateResult(name=n, passed=False, value=None, detail="x") for n in names
        ]
        by_name = {g.name: g for g in gates}
        for name, gate in overrides.items():
            by_name[name] = gate
        return [by_name[n] for n in names]

    def test_all_pass_is_100_percent(self) -> None:
        gates = self._seven_gates(
            **{
                n: GateResult(name=n, passed=True, value=True)
                for n in [
                    "session_open",
                    "cold_start_gap",
                    "environment_fitness",
                    "points_state",
                    "risk_validation",
                    "signal_confidence",
                    "execution",
                ]
            }
        )
        r = compute_trade_readiness(gates)
        self.assertEqual(r["pct"], 100)
        self.assertEqual(r["remaining_pct"], 0)

    def test_seven_zeros(self) -> None:
        gates = self._seven_gates()
        r = compute_trade_readiness(gates)
        self.assertEqual(r["pct"], 0)
        self.assertEqual(r["remaining_pct"], 100)

    def test_mixed_binary_and_continuous(self) -> None:
        gates = self._seven_gates(
            session_open=GateResult(name="session_open", passed=True),
            environment_fitness=GateResult(
                name="environment_fitness",
                passed=False,
                value={"score": 20},
            ),
            signal_confidence=GateResult(
                name="signal_confidence",
                passed=False,
                value={"confidence": 40.0, "threshold": 80.0},
            ),
        )
        # fitness=20/55=0.364, signal=40/80=0.5, session=1.0 → total=1.864/7 ≈ 27%
        r = compute_trade_readiness(gates, fitness_min=GATE_PASS_MIN)
        self.assertEqual(r["pct"], 27)
        self.assertEqual(r["remaining_pct"], 73)

    def test_dict_gates_from_snapshot(self) -> None:
        gates = [
            {"name": "session_open", "pass": True, "value": True},
            {"name": "cold_start_gap", "pass": False, "value": {"cold": True, "gap": False, "bars": 6}},
            {"name": "environment_fitness", "pass": False, "value": {"score": 40}},
            {"name": "points_state", "pass": False},
            {"name": "risk_validation", "pass": False},
            {"name": "signal_confidence", "pass": False, "value": {"confidence": 80, "threshold": 80}},
            {"name": "execution", "pass": False},
        ]
        # session=1.0, cold(bars=6)=1.0, env(40/55)=0.727, points=0, risk=0, sig(80/80)=1.0, exec=0
        # total=3.727/7 ≈ 53%
        r = compute_trade_readiness(gates)
        self.assertEqual(r["pct"], 53)
        self.assertEqual(r["remaining_pct"], 47)


class TestFormatHealthBadgeText(unittest.TestCase):
    def test_ready_string(self) -> None:
        self.assertEqual(
            format_health_badge_text("READY", {"pct": 100, "remaining_pct": 0}),
            "READY 100%",
        )

    def test_blocked_string(self) -> None:
        text = format_health_badge_text(
            "BLOCKED",
            {"pct": 71, "remaining_pct": 29},
        )
        self.assertEqual(text, "BLOCKED 71% — 29% remaining before trade")


if __name__ == "__main__":
    unittest.main()
