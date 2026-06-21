"""
Grid Search + Walk-Forward Optimization across production tick archive.

Double-hardened calibration: chaos slippage/latency + 80% OOS walk-forward gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

DEFAULT_ARCHIVE = Path("src/simulation/data/production_5day_archive.jsonl")
RESULTS_NAME = "matrix_search_results.jsonl"
BEST_OVERLAY_NAME = "best_matrix_overlay.json"
TARGET_WIN_RATE = 0.80
TARGET_SHARPE = 2.0
MIN_OOS_TRADES = 5


def _results_path(root: Path) -> Path:
    return root / "analytics" / RESULTS_NAME


def _best_overlay_paths() -> list[Path]:
    repo = Path(__file__).resolve().parents[2]
    paths = [
        Path(__file__).resolve().parent / "data" / BEST_OVERLAY_NAME,
        repo / "analytics" / "optimization_overlay.json",
    ]
    try:
        from system.testbed_firewall import testbed_root

        paths.append(testbed_root() / "analytics" / "optimization_overlay.json")
    except Exception:
        pass
    return paths


def _log_result(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def _print_ranked(
    label: str,
    ranked: list[tuple[Any, Any]],
    *,
    limit: int = 10,
    show_expectancy: bool = False,
) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {label}")
    print(f"{'=' * 72}")
    for idx, (params, metrics) in enumerate(ranked[:limit], start=1):
        vec = asdict(params)
        exp = ""
        if show_expectancy and hasattr(metrics, "expectancy_per_trade"):
            exp = f" Exp={metrics.expectancy_per_trade:.2f}"
        print(
            f"#{idx:02d} WR={metrics.win_rate:.1%} Sharpe={metrics.sharpe_ratio:.2f} "
            f"PF={metrics.profit_factor:.2f} DD={metrics.max_drawdown:.1f} "
            f"Trades={metrics.closed_trades}{exp} | "
            f"thr={vec['signal_threshold']:.0f} rsi_buy={vec['rsi_buy_min']:.0f} "
            f"rsi_sell={vec['rsi_sell_max']:.0f} RR={vec['reward_multiple']:.1f} "
            f"mom={vec['momentum_gap_points']:.0f}"
        )
    print(f"{'=' * 72}\n")


def _is_fragile(is_metrics: Any, oos_metrics: Any) -> bool:
    """Reject high IS performers that fail the 80% OOS survival gate."""
    if is_metrics.closed_trades < 10:
        return True
    if is_metrics.win_rate >= 0.75 and oos_metrics.win_rate < TARGET_WIN_RATE:
        return True
    if oos_metrics.win_rate < TARGET_WIN_RATE:
        return True
    if oos_metrics.closed_trades < MIN_OOS_TRADES:
        return True
    return False


def run_grid_search(
    archive: Path,
    *,
    reduced: bool = False,
    calibration: bool = True,
    max_permutations: int | None = None,
    results_root: Path | None = None,
) -> tuple[list[tuple[Any, Any]], Any, list[tuple[Any, Any]]]:
    from simulation.matrix_backtest import run_matrix_backtest
    from simulation.strategy_param_matrix import ParamVector, grid_size, iter_grid
    from simulation.walkforward_slices import slice_summary, walkforward_80_20

    os.environ.setdefault("IG_MATRIX_OPTIMIZATION", "1")
    os.environ.setdefault("IG_OPTIMIZATION_CHAOS", "1")

    slices = walkforward_80_20(archive)
    summary = slice_summary(slices)
    log_engine(f"matrix_search: walk-forward chaos-calibration {summary}")

    root = results_root or Path("src/simulation/data")
    results_file = _results_path(root)
    if results_file.exists():
        results_file.unlink()

    total = grid_size(reduced=reduced, calibration=calibration)
    if max_permutations:
        total = min(total, max_permutations)
    mode = "calibration" if calibration else ("reduced" if reduced else "full")
    print(f"Grid permutations planned: {total} (mode={mode}, chaos=ON)")
    print(f"In-sample: {summary['in_sample_ticks']} ticks ({summary['in_sample_days']})")
    print(f"OOS:       {summary['out_of_sample_ticks']} ticks ({summary['out_of_sample_days']})")
    print(f"OOS survival gate: WR≥{TARGET_WIN_RATE:.0%}, min trades={MIN_OOS_TRADES}")

    ranked_is: list[tuple[ParamVector, Any]] = []
    oos_survivors: list[tuple[ParamVector, Any, Any]] = []
    t0 = time.monotonic()
    for n, params in enumerate(
        iter_grid(
            reduced=reduced,
            calibration=calibration,
            max_permutations=max_permutations,
        ),
        start=1,
    ):
        is_metrics = run_matrix_backtest(slices.in_sample, params, chaos=True)
        oos_metrics = run_matrix_backtest(slices.out_of_sample, params, chaos=True)
        row = {
            "phase": "in_sample",
            "permutation": n,
            "params": asdict(params),
            "metrics": asdict(is_metrics),
            "score": is_metrics.score(),
            "chaos": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _log_result(results_file, row)
        oos_row = {
            "phase": "out_of_sample",
            "permutation": n,
            "params": asdict(params),
            "in_sample_metrics": asdict(is_metrics),
            "metrics": asdict(oos_metrics),
            "score": oos_metrics.score(),
            "chaos": True,
            "fragile": _is_fragile(is_metrics, oos_metrics),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _log_result(results_file, oos_row)

        if not _is_fragile(is_metrics, oos_metrics):
            oos_survivors.append((params, is_metrics, oos_metrics))
            oos_survivors.sort(
                key=lambda x: (
                    x[2].win_rate,
                    x[2].expectancy_per_trade,
                    x[2].sharpe_ratio,
                ),
                reverse=True,
            )

        ranked_is.append((params, is_metrics))
        ranked_is.sort(key=lambda x: x[1].score(), reverse=True)

        if n % 100 == 0 or n == total:
            elapsed = time.monotonic() - t0
            best_oos = oos_survivors[0][2] if oos_survivors else None
            oos_wr = f"{best_oos.win_rate:.1%}" if best_oos else "—"
            print(
                f"[{n}/{total}] elapsed={elapsed:.1f}s survivors={len(oos_survivors)} "
                f"best_OOS_WR={oos_wr}"
            )

    print("\n>>> PHASE 3/4: OOS survivors (≥80% WR under chaos slippage)\n")
    oos_ranked = [(p, o) for p, _is, o in oos_survivors]
    _print_ranked("FINAL OOS SURVIVORS", oos_ranked, limit=5, show_expectancy=True)

    winner: tuple[ParamVector, Any] | None = None
    for params, _is, oos in oos_survivors:
        if (
            oos.closed_trades >= MIN_OOS_TRADES
            and oos.win_rate >= TARGET_WIN_RATE
            and oos.sharpe_ratio >= TARGET_SHARPE
        ):
            winner = (params, oos)
            break

    if winner:
        params, oos = winner
        overlay = params.to_overlay()
        overlay["matrix_search"] = {
            **overlay.get("matrix_search", {}),
            "source": "calibration_chaos",
            "chaos_hardened": True,
            "validated": {
                "win_rate": oos.win_rate,
                "sharpe_ratio": oos.sharpe_ratio,
                "profit_factor": oos.profit_factor,
                "max_drawdown": oos.max_drawdown,
                "closed_trades": oos.closed_trades,
                "expectancy_per_trade": oos.expectancy_per_trade,
                "validated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        for path in _best_overlay_paths():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(overlay, indent=2) + "\n", encoding="utf-8")
            log_engine(f"matrix_search: chaos winner injected → {path}")
            print(f"✓ Winner injected → {path}")
        print("\n>>> WINNING PARAMETER VECTOR <<<")
        vec = asdict(params)
        for k, v in vec.items():
            print(f"  {k}: {v}")
        print(
            f"\n  OOS WR={oos.win_rate:.1%}  Sharpe={oos.sharpe_ratio:.2f}  "
            f"PF={oos.profit_factor:.2f}  DD={oos.max_drawdown:.1f}  "
            f"Trades={oos.closed_trades}  Expectancy={oos.expectancy_per_trade:.3f}"
        )
    else:
        print(
            f"No configuration met chaos-hardened OOS targets "
            f"(WR≥{TARGET_WIN_RATE:.0%}, Sharpe≥{TARGET_SHARPE}, "
            f"trades≥{MIN_OOS_TRADES}). Results: {results_file}"
        )

    return ranked_is, slices, oos_ranked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chaos-hardened grid + walk-forward search")
    parser.add_argument("--input", "-i", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--reduced", action="store_true", help="Legacy reduced grid")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full STRATEGY_PARAM_MATRIX grid (not calibration bounds)",
    )
    parser.add_argument("--max", type=int, default=None, help="Cap permutations")
    parser.add_argument(
        "--results-root",
        type=Path,
        default=Path("src/simulation/data"),
    )
    args = parser.parse_args(argv)

    if not args.input.is_file():
        print(f"Archive not found: {args.input}", file=sys.stderr)
        return 2

    os.environ.setdefault("IG_APEX_RUNTIME_MODE", "HARDENED_TESTBED")
    os.environ.setdefault("IG_MATRIX_OPTIMIZATION", "1")

    run_grid_search(
        args.input,
        reduced=args.reduced,
        calibration=not args.full and not args.reduced,
        max_permutations=args.max,
        results_root=args.results_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
