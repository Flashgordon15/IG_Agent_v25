"""
Autonomous ML optimization controller for HARDENED_TESTBED historical replay.

Orchestrates: testbed agent boot → high-speed replay → ledger/state monitoring →
post-run evaluation (win rate, max drawdown, slippage) → parameter self-healing →
repeat until win rate exceeds target across the full archive.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

TARGET_WIN_RATE = 0.70
MIN_TRADES_FOR_PASS = 5
MIN_REPLAY_TICKS = 10_000
DEFAULT_ARCHIVE = Path("src/simulation/data/production_5day_archive.jsonl")
DEFAULT_SPEED = 100.0
DEFAULT_MAX_CYCLES = 200
SETTLE_AFTER_REPLAY_SEC = 15.0
AGENT_BOOT_TIMEOUT_SEC = 120.0
REPLAY_WAIT_TIMEOUT_SEC = 600.0

OVERLAY_NAME = "optimization_overlay.json"
ML_META_REL = Path("ml_model") / "meta.json"
HISTORY_NAME = "optimization_history.jsonl"
STATE_NAME = "optimization_state.json"


@dataclass
class ClosedTradeRow:
    deal_ref: str
    epic: str
    side: str
    opened_at: str
    closed_at: str
    entry: float
    exit: float
    pnl: float
    result: str
    confidence: float
    setup_key: str
    slippage_pts: float = 0.0


@dataclass
class EvaluationMatrix:
    cycle: int
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown_gbp: float
    avg_slippage_pts: float
    total_pnl_gbp: float
    replay_ticks: int
    passed: bool
    notes: str = ""


@dataclass
class TuningKnobs:
    signal_threshold_floor: float = 42.0
    rsi_buy_min: int = 58
    rsi_sell_max: int = 45
    reward_multiple: float = 3.0
    momentum_gap_points: int = 20
    stop_tighten_pct: float = 0.0
    min_adjusted_score: float = 80.0
    max_spread: float = 35.0

    @classmethod
    def from_overlay(cls, data: dict[str, Any]) -> TuningKnobs:
        pl = data.get("protective_learning") or {}
        return cls(
            signal_threshold_floor=float(
                pl.get("signal_threshold_floor", data.get("signal_threshold", 42))
            ),
            rsi_buy_min=int(data.get("rsi_buy_min", 58)),
            rsi_sell_max=int(data.get("rsi_sell_max", 45)),
            reward_multiple=float(data.get("reward_multiple", 3.0)),
            momentum_gap_points=int(data.get("momentum_gap_points", 20)),
            stop_tighten_pct=float(data.get("stop_tighten_pct", 0.0)),
            min_adjusted_score=float(
                (data.get("ml_filter_overrides") or {}).get("min_adjusted_score", 80.0)
            ),
            max_spread=float(data.get("max_spread", 35.0)),
        )

    def to_overlay(self) -> dict[str, Any]:
        return {
            "signal_threshold": self.signal_threshold_floor,
            "rsi_buy_min": self.rsi_buy_min,
            "rsi_sell_max": self.rsi_sell_max,
            "reward_multiple": round(self.reward_multiple, 2),
            "momentum_gap_points": self.momentum_gap_points,
            "max_spread": round(self.max_spread, 1),
            "stop_tighten_pct": round(self.stop_tighten_pct, 4),
            "protective_learning": {
                "signal_threshold_floor": round(self.signal_threshold_floor, 1),
            },
            "ml_filter_overrides": {
                "min_adjusted_score": round(self.min_adjusted_score, 2),
            },
        }


class TestbedLedgerMonitor:
    """Real-time reads from isolated testbed ledger + state."""

    def __init__(self, ledger_path: Path, state_path: Path) -> None:
        self.ledger_path = ledger_path
        self.state_path = state_path

    def closed_trades(self) -> list[ClosedTradeRow]:
        if not self.ledger_path.is_file():
            return []
        rows: list[ClosedTradeRow] = []
        try:
            with sqlite3.connect(str(self.ledger_path)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT opened_at, closed_at, epic, side, entry, exit, size,
                           pnl_points, ig_pnl_currency, result, confidence,
                           setup_key, deal_reference, ig_deal_id, notes
                    FROM trades
                    WHERE closed_at IS NOT NULL AND closed_at != ''
                    ORDER BY closed_at ASC
                    """
                )
                for r in cur.fetchall():
                    pnl = r["ig_pnl_currency"]
                    if pnl is None:
                        pnl = r["pnl_points"]
                    pnl_f = float(pnl or 0.0)
                    result = str(r["result"] or "").upper()
                    if not result:
                        result = "WIN" if pnl_f > 0 else "LOSS" if pnl_f < 0 else "BE"
                    deal_ref = str(r["deal_reference"] or r["ig_deal_id"] or "")
                    slip = 0.0
                    notes = str(r["notes"] or "")
                    if "slippage" in notes.lower():
                        try:
                            for part in notes.split():
                                if part.replace(".", "", 1).isdigit():
                                    slip = float(part)
                                    break
                        except ValueError:
                            pass
                    rows.append(
                        ClosedTradeRow(
                            deal_ref=deal_ref,
                            epic=str(r["epic"] or ""),
                            side=str(r["side"] or ""),
                            opened_at=str(r["opened_at"] or ""),
                            closed_at=str(r["closed_at"] or ""),
                            entry=float(r["entry"] or 0),
                            exit=float(r["exit"] or 0),
                            pnl=pnl_f,
                            result=result,
                            confidence=float(r["confidence"] or 0),
                            setup_key=str(r["setup_key"] or ""),
                            slippage_pts=slip,
                        )
                    )
        except sqlite3.Error as exc:
            log_engine(f"OptimizationManager ledger read: {type(exc).__name__}: {exc}")
        return rows

    def fill_count(self) -> int:
        if not self.ledger_path.is_file():
            return 0
        try:
            with sqlite3.connect(str(self.ledger_path)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM testbed_fills"
                ).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def read_state_snapshot(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


class EvaluationEngine:
    """Post-run metrics matrix from closed trades + fills."""

    def __init__(
        self,
        monitor: TestbedLedgerMonitor,
        *,
        target_win_rate: float = TARGET_WIN_RATE,
        min_trades: int = MIN_TRADES_FOR_PASS,
    ) -> None:
        self._monitor = monitor
        self._target = target_win_rate
        self._min_trades = min_trades

    def evaluate(self, *, cycle: int, replay_ticks: int = 0) -> EvaluationMatrix:
        trades = self._monitor.closed_trades()
        wins = sum(1 for t in trades if t.result == "WIN" or t.pnl > 0)
        losses = sum(1 for t in trades if t.result == "LOSS" or t.pnl < 0)
        closed = len(trades)
        win_rate = (wins / closed) if closed else 0.0

        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            equity += t.pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        slips = [t.slippage_pts for t in trades if t.slippage_pts > 0]
        avg_slip = sum(slips) / len(slips) if slips else 0.0
        total_pnl = sum(t.pnl for t in trades)

        passed = (
            closed >= self._min_trades and win_rate >= self._target
        )
        notes = ""
        if closed < self._min_trades:
            notes = f"insufficient closed trades ({closed}<{self._min_trades})"
        elif win_rate < self._target:
            notes = f"win_rate {win_rate:.1%} below target {self._target:.0%}"

        return EvaluationMatrix(
            cycle=cycle,
            closed_trades=closed,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            max_drawdown_gbp=max_dd,
            avg_slippage_pts=avg_slip,
            total_pnl_gbp=total_pnl,
            replay_ticks=replay_ticks,
            passed=passed,
            notes=notes,
        )


class ParameterTuner:
    """Algorithmic self-healing — adjusts testbed-isolated overlay + ML meta."""

    def __init__(self, analytics_dir: Path) -> None:
        self._analytics = analytics_dir
        self._overlay_path = analytics_dir / OVERLAY_NAME
        self._ml_meta_path = analytics_dir.parent / "data" / ML_META_REL

    def load_knobs(self) -> TuningKnobs:
        if self._overlay_path.is_file():
            try:
                data = json.loads(self._overlay_path.read_text(encoding="utf-8"))
                return TuningKnobs.from_overlay(data)
            except (json.JSONDecodeError, OSError):
                pass
        return TuningKnobs()

    def seed_ml_meta(self, repo_root: Path) -> None:
        src = repo_root / "src" / "data" / "ml_model" / "meta.json"
        self._ml_meta_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._ml_meta_path.is_file() and src.is_file():
            self._ml_meta_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def apply(
        self,
        metrics: EvaluationMatrix,
        failed_trades: list[ClosedTradeRow],
        *,
        replay_ticks: int = 0,
    ) -> TuningKnobs | None:
        if replay_ticks < MIN_REPLAY_TICKS:
            log_engine(
                f"OptimizationManager tune: SKIPPED — replay_ticks={replay_ticks} "
                f"< minimum {MIN_REPLAY_TICKS} (fixture too small to tune safely)"
            )
            return None
        knobs = self.load_knobs()
        loss_rows = [
            t
            for t in failed_trades
            if t.result == "LOSS" or t.pnl < 0
        ]

        if metrics.closed_trades < MIN_TRADES_FOR_PASS:
            knobs.signal_threshold_floor = max(38.0, knobs.signal_threshold_floor - 2.0)
            knobs.min_adjusted_score = max(70.0, knobs.min_adjusted_score - 3.0)
            log_engine(
                "OptimizationManager tune: sparse trades — loosening entry gates"
            )
        elif metrics.win_rate < TARGET_WIN_RATE:
            knobs.signal_threshold_floor = min(75.0, knobs.signal_threshold_floor + 2.0)
            knobs.min_adjusted_score = min(110.0, knobs.min_adjusted_score + 2.5)
            knobs.rsi_buy_min = min(68, knobs.rsi_buy_min + 1)
            knobs.rsi_sell_max = max(38, knobs.rsi_sell_max - 1)
            knobs.stop_tighten_pct = min(0.25, knobs.stop_tighten_pct + 0.04)
            if metrics.max_drawdown_gbp > 50:
                knobs.reward_multiple = min(4.5, knobs.reward_multiple + 0.15)
            else:
                knobs.momentum_gap_points = max(10, knobs.momentum_gap_points - 2)
            if metrics.avg_slippage_pts > 1.0:
                knobs.max_spread = max(15.0, knobs.max_spread - 2.0)
            log_engine(
                f"OptimizationManager tune: win_rate={metrics.win_rate:.1%} "
                f"losses={len(loss_rows)} — tightening filters / stops"
            )

        self._write_overlay(knobs)
        self._write_ml_meta(knobs, loss_rows)
        self._backlearn_losses(loss_rows)
        return knobs

    def _write_overlay(self, knobs: TuningKnobs) -> None:
        self._analytics.mkdir(parents=True, exist_ok=True)
        payload = knobs.to_overlay()
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = self._overlay_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._overlay_path)

    def _write_ml_meta(
        self, knobs: TuningKnobs, loss_rows: list[ClosedTradeRow]
    ) -> None:
        self._ml_meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {}
        if self._ml_meta_path.is_file():
            try:
                meta = json.loads(self._ml_meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        meta.setdefault("features", ["adjusted_score", "raw_score", "rsi", "atr_ratio"])
        meta["filter_overrides"] = {
            "min_adjusted_score": knobs.min_adjusted_score,
        }
        meta["testbed_optimization"] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "loss_timestamps": [t.closed_at for t in loss_rows[:50]],
            "failed_setups": list({t.setup_key for t in loss_rows if t.setup_key}),
            "knobs": asdict(knobs),
        }
        tmp = self._ml_meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._ml_meta_path)

    def _backlearn_losses(self, loss_rows: list[ClosedTradeRow]) -> None:
        try:
            from trading.continuous_optimization_worker import (
                get_continuous_optimization_worker,
            )

            worker = get_continuous_optimization_worker()
            for row in loss_rows:
                if row.deal_ref:
                    worker.on_trade_closed(
                        deal_ref=row.deal_ref,
                        result="LOSS",
                        net_pnl=row.pnl,
                    )
        except Exception as exc:
            log_engine(
                f"OptimizationManager back-learn skipped: {type(exc).__name__}: {exc}"
            )


class OptimizationManager:
    """Autonomous testbed calibration loop controller."""

    def __init__(
        self,
        *,
        replay_path: Path,
        speed: float = DEFAULT_SPEED,
        target_win_rate: float = TARGET_WIN_RATE,
        max_cycles: int = DEFAULT_MAX_CYCLES,
        project_root: Path | None = None,
        testbed_root: Path | None = None,
        agent_enabled: bool = True,
    ) -> None:
        self._replay_path = replay_path.resolve()
        self._validate_archive(self._replay_path)
        self._speed = speed
        self._target = target_win_rate
        self._max_cycles = max_cycles
        self._project_root = project_root or Path(__file__).resolve().parents[2]
        self._testbed_root = testbed_root
        self._agent_enabled = agent_enabled
        self._stop = threading.Event()
        self._agent_proc: subprocess.Popen[Any] | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _validate_archive(path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"replay archive not found: {path}")
        count = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
                    if count >= MIN_REPLAY_TICKS:
                        break
        if count < MIN_REPLAY_TICKS:
            raise ValueError(
                f"replay archive {path.name} has {count} ticks — "
                f"minimum {MIN_REPLAY_TICKS} required for optimization"
            )
        log_engine(
            f"OptimizationManager: archive validated {path.name} "
            f"(≥{MIN_REPLAY_TICKS} ticks)"
        )

    def _arm(self) -> None:
        from system.apex_runtime_mode import apply_runtime_mode_to_environ
        from system.testbed_firewall import (
            arm_testbed_firewall,
            testbed_ledger_path,
            testbed_state_path,
        )

        os.environ["IG_APEX_RUNTIME_MODE"] = "HARDENED_TESTBED"
        if self._testbed_root:
            os.environ["IG_TESTBED_ROOT"] = str(self._testbed_root)
        apply_runtime_mode_to_environ()
        arm_testbed_firewall()
        self._ledger = testbed_ledger_path()
        self._state = testbed_state_path()
        self._analytics = self._ledger.parent.parent / "analytics"
        self._monitor = TestbedLedgerMonitor(self._ledger, self._state)
        self._evaluator = EvaluationEngine(
            self._monitor, target_win_rate=self._target
        )
        self._analytics.mkdir(parents=True, exist_ok=True)
        self._tuner = ParameterTuner(self._analytics)
        self._tuner.seed_ml_meta(self._project_root)

    def _health_ok(self, port: int = 9199) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=2.0
            ) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _wait_for_health(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            proc = self._agent_proc
            if proc is not None and proc.poll() is not None:
                log_engine(
                    f"OptimizationManager: agent exited early rc={proc.returncode}"
                )
                return False
            if self._health_ok():
                return True
            time.sleep(1.0)
        return self._health_ok()

    def _replay_complete(self) -> tuple[bool, int]:
        from system.testbed_firewall import testbed_root

        marker = testbed_root() / "replay" / ".replay_complete"
        if not marker.is_file():
            return False, 0
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            return True, int(data.get("emitted") or 0)
        except (json.JSONDecodeError, OSError):
            return marker.is_file(), 0

    def _wait_for_replay(self, timeout: float) -> tuple[bool, int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            done, emitted = self._replay_complete()
            if done:
                return True, emitted
            time.sleep(0.5)
        return self._replay_complete()

    def _start_agent(self) -> bool:
        if not self._agent_enabled:
            return False
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(self._project_root / "src"),
                "IG_APEX_RUNTIME_MODE": "HARDENED_TESTBED",
                "IG_HISTORICAL_REPLAY": str(self._replay_path.resolve()),
                "IG_REPLAY_SPEED": str(self._speed),
                "IG_MULTI_API_BROKER": "0",
            }
        )
        if self._testbed_root:
            env["IG_TESTBED_ROOT"] = str(self._testbed_root)
        main_py = self._project_root / "src" / "main.py"
        if not main_py.is_file():
            log_engine("OptimizationManager: main.py missing — agent disabled")
            return False
        try:
            self._agent_proc = subprocess.Popen(
                [sys.executable, str(main_py)],
                cwd=str(self._project_root),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log_engine(
                f"OptimizationManager: testbed agent pid={self._agent_proc.pid} "
                f"port=9199 speed={self._speed}x"
            )
            return True
        except OSError as exc:
            log_engine(
                f"OptimizationManager: agent start failed {type(exc).__name__}: {exc}"
            )
            return False

    def _stop_agent(self) -> None:
        proc = self._agent_proc
        self._agent_proc = None
        if proc is None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass

    def _run_replay_standalone(self) -> int:
        from simulation.historical_replayer import (
            build_replayer,
            clear_replay_completion_marker,
        )

        clear_replay_completion_marker()
        replayer = build_replayer(
            self._replay_path,
            speed=self._speed,
        )
        return replayer.run_blocking()

    def run_cycle(self, cycle: int) -> EvaluationMatrix:
        from simulation.historical_replayer import clear_replay_completion_marker

        clear_replay_completion_marker()
        agent_started = self._start_agent()
        replay_ticks = 0

        if agent_started:
            if not self._wait_for_health(AGENT_BOOT_TIMEOUT_SEC):
                log_engine(
                    "OptimizationManager: agent health timeout — standalone replay"
                )
                self._stop_agent()
                replay_ticks = self._run_replay_standalone()
            else:
                ok, replay_ticks = self._wait_for_replay(REPLAY_WAIT_TIMEOUT_SEC)
                if not ok:
                    log_engine("OptimizationManager: replay wait timeout")
                time.sleep(SETTLE_AFTER_REPLAY_SEC)
                self._stop_agent()
        else:
            replay_ticks = self._run_replay_standalone()

        metrics = self._evaluator.evaluate(cycle=cycle, replay_ticks=replay_ticks)
        self._persist_history(metrics)
        log_engine(
            f"OptimizationManager cycle={cycle} win_rate={metrics.win_rate:.1%} "
            f"closed={metrics.closed_trades} max_dd={metrics.max_drawdown_gbp:.2f} "
            f"slippage={metrics.avg_slippage_pts:.3f} passed={metrics.passed}"
        )
        return metrics

    def _persist_history(self, metrics: EvaluationMatrix) -> None:
        self._analytics.mkdir(parents=True, exist_ok=True)
        path = self._analytics / HISTORY_NAME
        row = asdict(metrics)
        row["timestamp"] = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        state_path = self._analytics / STATE_NAME
        state_path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")

    def run_loop(self) -> EvaluationMatrix:
        self._arm()
        log_engine(
            f"OptimizationManager: autonomous loop armed archive={self._replay_path.name} "
            f"target_win_rate={self._target:.0%} max_cycles={self._max_cycles}"
        )
        last = EvaluationMatrix(
            cycle=0,
            closed_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            max_drawdown_gbp=0.0,
            avg_slippage_pts=0.0,
            total_pnl_gbp=0.0,
            replay_ticks=0,
            passed=False,
        )
        for cycle in range(1, self._max_cycles + 1):
            if self._stop.is_set():
                break
            last = self.run_cycle(cycle)
            if last.passed:
                log_engine(
                    f"OptimizationManager: TARGET MET — win_rate={last.win_rate:.1%} "
                    f"after {cycle} cycle(s)"
                )
                break
            failed = [
                t
                for t in self._monitor.closed_trades()
                if t.result == "LOSS" or t.pnl < 0
            ]
            knobs = self._tuner.apply(last, failed, replay_ticks=last.replay_ticks)
            if knobs is not None:
                log_engine(
                    f"OptimizationManager: next cycle knobs "
                    f"threshold={knobs.signal_threshold_floor:.1f} "
                    f"rsi_buy={knobs.rsi_buy_min} reward={knobs.reward_multiple:.2f}"
                )
        else:
            log_engine(
                f"OptimizationManager: max_cycles={self._max_cycles} reached "
                f"without verified {self._target:.0%} win rate"
            )
        return last

    def start_background(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        def _runner() -> None:
            try:
                self.run_loop()
            except Exception as exc:
                log_engine(
                    f"OptimizationManager loop error: {type(exc).__name__}: {exc}"
                )
            finally:
                self._stop_agent()

        self._thread = threading.Thread(
            target=_runner,
            name="optimization-manager",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        self._stop_agent()


def default_replay_archive(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[2]
    prod = root / DEFAULT_ARCHIVE
    if prod.is_file():
        return prod
    return root / "src" / "simulation" / "data" / "sample_ticks.jsonl"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Autonomous testbed ML optimization loop (HARDENED_TESTBED)"
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=default_replay_archive(root),
        help="Historical tick archive (default: production_5day_archive.jsonl)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=float(os.environ.get("IG_REPLAY_SPEED", str(DEFAULT_SPEED))),
    )
    parser.add_argument(
        "--target-win-rate",
        type=float,
        default=TARGET_WIN_RATE,
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=int(os.environ.get("IG_OPTIMIZATION_MAX_CYCLES", str(DEFAULT_MAX_CYCLES))),
    )
    parser.add_argument(
        "--testbed-root",
        type=Path,
        default=None,
        help="Isolated testbed root (default: IG_TESTBED_ROOT or apex isolated)",
    )
    parser.add_argument(
        "--no-agent",
        action="store_true",
        help="Replay-only mode (no main.py subprocess)",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Run autonomous loop (blocks until target met or max cycles)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mgr = OptimizationManager(
        replay_path=args.input,
        speed=float(args.speed),
        target_win_rate=float(args.target_win_rate),
        max_cycles=int(args.max_cycles),
        testbed_root=args.testbed_root,
        agent_enabled=not args.no_agent,
    )
    if args.start:
        result = mgr.run_loop()
        return 0 if result.passed else 1
    print("Pass --start to launch the autonomous calibration loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
