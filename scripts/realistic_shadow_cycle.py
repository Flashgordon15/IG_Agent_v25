#!/usr/bin/env python3
"""In-process realistic shadow cycle — decision_engine → ShadowExecutor → forward MTM.

Uses real replay_results.jsonl bars (already forward-labeled). Never contacts the
broker. Writes an isolated shadow ledger + favour JSON under --data-root.

Safe while production twins are paused / A2-blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_replay(limit: int | None) -> list[dict[str, Any]]:
    from system.paths import data_dir, project_root

    candidates = [
        data_dir() / "replay_results.jsonl",
        project_root() / "src" / "data" / "v31-production" / "replay_results.jsonl",
        project_root() / "src" / "data" / "replay_results.jsonl",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            # Prefer fired setups with a directional label.
            if not row.get("fired") and row.get("label_3bar") is None and row.get("label_6bar") is None:
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def _style_for_horizon(horizon: str) -> str:
    return "sniper" if horizon == "3bar" else "long"


def _label_key(horizon: str) -> str:
    return "label_3bar" if horizon == "3bar" else "label_6bar"


def _pnl_pts_for_label(label: str, stop_pts: float = 20.0) -> float:
    lab = str(label or "").upper()
    if lab in ("WIN", "W", "1", "TRUE"):
        return float(stop_pts)  # 1R
    if lab in ("LOSS", "L", "0", "FALSE"):
        return -float(stop_pts)
    return 0.0


def _run_cycle(
    *,
    data_root: Path,
    limit: int,
    ml_floor: float,
    size: float,
    offline_feed_ok: bool = True,
) -> dict[str, Any]:
    os.environ.setdefault("IG_TEST_HARNESS", "1")
    os.environ.setdefault("USE_ML_SIGNAL", "1")

    from execution.types import TradeSignal
    from ml.decision_engine import blend_ml_confidence
    from system.config_loader import get_config
    from trading.shadow_executor import ShadowExecutor
    from unittest.mock import patch

    # Point shadow ledger into isolated root
    import trading.shadow_executor as se

    ledger = data_root / "shadow_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if ledger.exists():
        ledger.write_text("", encoding="utf-8")
    se.shadow_ledger_path = lambda: ledger  # type: ignore[assignment]
    se._OPEN_POSITIONS.clear()

    cfg = get_config()
    # Force ML path on for this cycle even if overlay toggles it.
    try:
        cfg._data["USE_ML_SIGNAL"] = True  # type: ignore[attr-defined]
    except Exception:
        pass

    rows = _load_replay(limit)
    executor = ShadowExecutor()

    # Weekend / offline: feed_quality vetoes everything when hub is offline.
    # Bypass only the feed layer so ML + forward labels can score favour.
    _feed_patches: list[Any] = []
    if offline_feed_ok:
        class _FeedOK:
            veto = False
            penalty_pts = 0.0
            reason = "offline_feed_ok"

        _feed_patches.append(
            patch("ml.feed_quality.evaluate_feed_quality", return_value=_FeedOK())
        )
    for p in _feed_patches:
        p.start()
    try:
        return _run_cycle_body(
            cfg=cfg,
            rows=rows,
            executor=executor,
            ledger=ledger,
            data_root=data_root,
            ml_floor=ml_floor,
            size=size,
            offline_feed_ok=offline_feed_ok,
        )
    finally:
        for p in reversed(_feed_patches):
            p.stop()


def _run_cycle_body(
    *,
    cfg: Any,
    rows: list[dict[str, Any]],
    executor: Any,
    ledger: Path,
    data_root: Path,
    ml_floor: float,
    size: float,
    offline_feed_ok: bool,
) -> dict[str, Any]:
    from data.models import Quote as _Quote
    from execution.types import TradeSignal
    from ml.decision_engine import blend_ml_confidence

    taken: list[dict[str, Any]] = []
    vetoed: list[dict[str, Any]] = []
    by_style: dict[str, Counter] = defaultdict(Counter)
    by_epic: dict[str, Counter] = defaultdict(Counter)
    pnl_by_style: dict[str, float] = defaultdict(float)
    pnl_by_epic: dict[str, float] = defaultdict(float)

    for i, row in enumerate(rows):
        epic = str(row.get("epic") or row.get("instrument") or "").strip()
        if not epic:
            continue
        direction = str(row.get("direction") or row.get("side") or "BUY").upper()
        if direction not in ("BUY", "SELL"):
            direction = "BUY"
        setup_key = str(row.get("setup") or row.get("setup_key") or f"replay|{epic}|{i}")
        rules_conf = float(
            row.get("adjusted_score")
            or row.get("confidence")
            or row.get("raw_score")
            or 65.0
        )
        market = str(row.get("market") or epic)
        snapshot = {
            "raw_confidence": float(row.get("raw_score") or rules_conf),
            "last": {
                "rsi": float(row.get("rsi") or 50.0),
                "atr": float(row.get("atr") or 20.0),
            },
            "close_history": row.get("close_history") or [],
        }
        # Attach widened features if present on the row
        for k in (
            "adjusted_score",
            "raw_score",
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
            if row.get(k) is not None:
                snapshot[k] = row[k]

        quote = MagicMock(bid=100.0, offer=100.2, Bid=100.0, Offer=100.2)
        entry_px = float(row.get("close") or row.get("entry") or row.get("mid") or 100.0)
        if entry_px > 0:
            quote = MagicMock(
                bid=entry_px - 0.1,
                offer=entry_px + 0.1,
                Bid=entry_px - 0.1,
                Offer=entry_px + 0.1,
            )

        try:
            decision = blend_ml_confidence(
                cfg=cfg,
                market=market,
                direction=direction,
                snapshot=snapshot,
                store=None,
                rules_conf=rules_conf,
                setup_key=setup_key,
                quote=quote,
                epic=epic,
            )
        except Exception as exc:
            vetoed.append(
                {
                    "epic": epic,
                    "reason": f"decision_error:{type(exc).__name__}",
                    "setup_key": setup_key,
                }
            )
            continue

        ml_prob = decision.ml_prob
        conf = float(decision.confidence)
        veto = bool(decision.setup_veto or decision.feed_veto or conf <= 0.0)
        if not veto and ml_prob is not None and float(ml_prob) < ml_floor:
            veto = True
            reason = f"ml_floor {ml_prob:.3f} < {ml_floor}"
        else:
            reason = decision.notes or decision.mode

        # Dual horizon: score both sniper and long labels when present
        for horizon in ("3bar", "6bar"):
            lab = row.get(_label_key(horizon))
            if lab is None:
                continue
            style = _style_for_horizon(horizon)
            if veto:
                vetoed.append(
                    {
                        "epic": epic,
                        "style": style,
                        "reason": reason,
                        "ml_prob": ml_prob,
                        "confidence": conf,
                        "mode": decision.mode,
                        "label": lab,
                    }
                )
                by_style[style]["vetoed"] += 1
                by_epic[epic]["vetoed"] += 1
                continue

            from data.models import Quote as _Quote

            qobj = _Quote(
                datetime.now(timezone.utc),
                float(getattr(quote, "bid", entry_px - 0.1) or entry_px - 0.1),
                float(getattr(quote, "offer", entry_px + 0.1) or entry_px + 0.1),
            )
            signal = TradeSignal(
                market=market,
                epic=epic,
                direction=direction,
                raw_confidence=float(rules_conf),
                adjusted_confidence=float(conf),
                setup_key=setup_key,
                quote=qobj,
                snapshot=snapshot,
            )

            result = executor.execute(
                signal,
                {
                    "entry": entry_px,
                    "size": size,
                    "gate_approved_size": size,
                    "ml_score": ml_prob,
                    "style": style,
                    "horizon": horizon,
                },
                gate_snapshot={"ml_mode": decision.mode, "style": style},
            )
            if not result.success:
                by_style[style]["reject"] += 1
                continue

            pts = _pnl_pts_for_label(str(lab), stop_pts=20.0)
            gbp = pts * float(size)
            label_u = str(lab).upper()
            win = label_u in ("WIN", "W", "1", "TRUE")
            loss = label_u in ("LOSS", "L", "0", "FALSE")
            by_style[style]["taken"] += 1
            by_epic[epic]["taken"] += 1
            if win:
                by_style[style]["wins"] += 1
                by_epic[epic]["wins"] += 1
            elif loss:
                by_style[style]["losses"] += 1
                by_epic[epic]["losses"] += 1
            else:
                by_style[style]["be"] += 1
                by_epic[epic]["be"] += 1
            pnl_by_style[style] += gbp
            pnl_by_epic[epic] += gbp
            taken.append(
                {
                    "deal_id": result.deal_id,
                    "epic": epic,
                    "style": style,
                    "horizon": horizon,
                    "direction": direction,
                    "ml_prob": ml_prob,
                    "confidence": conf,
                    "mode": decision.mode,
                    "label": lab,
                    "pnl_gbp": gbp,
                }
            )

    def _wr(c: Counter) -> float | None:
        decided = int(c.get("wins", 0)) + int(c.get("losses", 0))
        if decided <= 0:
            return None
        return round(int(c.get("wins", 0)) / decided, 4)

    style_summary = {
        s: {
            **dict(c),
            "wr": _wr(c),
            "pnl_gbp": round(pnl_by_style[s], 2),
        }
        for s, c in by_style.items()
    }
    epic_summary = {
        e: {
            **dict(c),
            "wr": _wr(c),
            "pnl_gbp": round(pnl_by_epic[e], 2),
        }
        for e, c in by_epic.items()
    }

    favour = "EDGE_WEAK"
    sn = style_summary.get("sniper") or {}
    lg = style_summary.get("long") or {}
    sn_wr = sn.get("wr")
    lg_wr = lg.get("wr")
    notes: list[str] = []
    if sn_wr is not None and sn_wr >= 0.55 and float(sn.get("pnl_gbp") or 0) > 0:
        notes.append("sniper favours (WR≥55% + net+)")
        favour = "FAVOURS_SNIPER"
    if lg_wr is not None and lg_wr >= 0.55 and float(lg.get("pnl_gbp") or 0) > 0:
        notes.append("long favours (WR≥55% + net+)")
        favour = "FAVOURS_LONG" if favour == "EDGE_WEAK" else "FAVOURS_BOTH"
    if not notes:
        if (sn.get("taken") or 0) + (lg.get("taken") or 0) == 0:
            favour = "NO_TAKES"
            notes.append("ML/gates vetoed all candidates — selectivity high or features weak")
        else:
            favour = "NO_EDGE"
            notes.append("taken sample not above break-even favour thresholds")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_root": str(data_root),
        "ledger": str(ledger),
        "candidates": len(rows),
        "taken": len(taken),
        "vetoed": len(vetoed),
        "ml_floor": ml_floor,
        "size": size,
        "by_style": style_summary,
        "by_epic": epic_summary,
        "favour": favour,
        "favour_notes": notes,
        "offline_feed_ok": offline_feed_ok,
        "sample_taken": taken[:12],
        "sample_vetoed": vetoed[:12],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--day", default="")
    ap.add_argument("--ml-floor", type=float, default=0.52)
    ap.add_argument("--size", type=float, default=0.5)
    ap.add_argument(
        "--require-live-feed",
        action="store_true",
        help="Do not bypass feed_quality (default: offline_feed_ok for weekend)",
    )
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    report = _run_cycle(
        data_root=args.data_root,
        limit=args.limit,
        ml_floor=args.ml_floor,
        size=args.size,
        offline_feed_ok=not args.require_live_feed,
    )
    report["elapsed_sec"] = round(time.time() - t0, 2)
    if args.day:
        report["day"] = args.day

    text = json.dumps(report, indent=2)
    print(text)
    if args.write:
        out = args.data_root / "reports"
        out.mkdir(parents=True, exist_ok=True)
        day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = out / f"shadow_cycle_{day}.json"
        path.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
