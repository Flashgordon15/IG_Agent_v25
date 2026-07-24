"""Lightweight ML feedback loop — structured close outcomes for overnight scrape.

Appends one JSONL row per closed trade under
``data_dir()/metrics/ml_trade_outcomes.jsonl`` and logs a summary line the
overnight desk monitor can scrape.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_LOCK = threading.RLock()
_SEEN: set[str] = set()


def outcomes_path() -> Path:
    from system.paths import data_dir

    return Path(data_dir()) / "metrics" / "ml_trade_outcomes.jsonl"


def _refuse_prod_write_under_test(path: Path) -> bool:
    if not (
        os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        or os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    ):
        return False
    try:
        from system.paths import project_root

        resolved = path.resolve()
        prod = (project_root() / "src" / "data" / "v31-production").resolve()
        return str(resolved).startswith(str(prod) + os.sep) or resolved == prod
    except OSError:
        return False


def reset_ml_trade_outcomes_for_tests() -> None:
    with _LOCK:
        _SEEN.clear()


def resolve_ml_score_for_close(
    *,
    ml_score: float | None = None,
    deal_id: str = "",
    epic: str = "",
) -> float | None:
    """Prefer explicit score; else recover from sniper snapshot / autopsy."""
    if ml_score is not None:
        try:
            return float(ml_score)
        except (TypeError, ValueError):
            pass
    epic_s = str(epic or "").strip()
    deal = str(deal_id or "").strip()
    # Live sniper last-by-epic (Instant / Core B fills).
    try:
        from alpha.micro_sniper_ml import latest_sniper_ml_snapshot

        snap = latest_sniper_ml_snapshot(epic=epic_s or None)
        if isinstance(snap, dict) and snap.get("p_success") is not None:
            return float(snap["p_success"])
    except Exception:
        pass
    # Autopsy / learning store row written at entry.
    if deal:
        try:
            from system.paths import data_dir

            autopsy = Path(data_dir()) / "autopsy" / f"{deal}.json"
            if autopsy.is_file():
                raw = json.loads(autopsy.read_text(encoding="utf-8"))
                for key in ("ml_score_at_entry", "ml_score", "p_success", "confidence_at_entry"):
                    if raw.get(key) is not None:
                        return float(raw[key])
        except Exception:
            pass
    return None


def record_ml_trade_outcome(
    *,
    account_id: str = "",
    epic: str = "",
    side: str = "",
    ml_score: float | None = None,
    regime: str = "",
    style: str = "",
    pnl: float | None = None,
    deal_id: str = "",
    exit_reason: str = "",
    hold_sec: float | None = None,
    engine_origin: str = "",
    path: Path | None = None,
) -> bool:
    """Append one structured outcome. Idempotent on deal_id within process."""
    deal = str(deal_id or "").strip()
    if deal:
        with _LOCK:
            if deal in _SEEN:
                return False
            _SEEN.add(deal)
    resolved = resolve_ml_score_for_close(
        ml_score=ml_score, deal_id=deal, epic=str(epic or "")
    )
    row: dict[str, Any] = {
        "ts": time.time(),
        "account": str(account_id or ""),
        "epic": str(epic or ""),
        "side": str(side or "").upper(),
        "ml_score": None if resolved is None else round(float(resolved), 6),
        "ml_score_at_entry": None if resolved is None else round(float(resolved), 6),
        "regime": str(regime or ""),
        "market_regime": str(regime or ""),
        "style": str(style or ""),
        "pnl": None if pnl is None else round(float(pnl), 4),
        "deal_id": deal,
        "exit_reason": str(exit_reason or "")[:160],
        "hold_sec": None if hold_sec is None else round(float(hold_sec), 1),
        "hold_duration_seconds": None if hold_sec is None else round(float(hold_sec), 1),
        "engine_origin": str(engine_origin or ""),
    }
    out = path or outcomes_path()
    if _refuse_prod_write_under_test(out):
        return False
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as exc:
        log_engine(f"ml_trade_outcomes: write failed {type(exc).__name__}: {exc}")
        return False

    score_s = "na" if row["ml_score"] is None else f"{row['ml_score']:.3f}"
    pnl_s = "na" if row["pnl"] is None else f"{row['pnl']:.2f}"
    log_engine(
        f"ML_TRADE_OUTCOME account={row['account']} epic={row['epic'] or '-'} "
        f"side={row['side'] or '-'} style={row['style'] or 'unknown'} "
        f"ml_score={score_s} pnl={pnl_s} deal={deal[:16] or '-'}"
    )
    return True


def rolling_wr_by_score_bucket(
    *,
    path: Path | None = None,
    buckets: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9),
    max_rows: int = 500,
) -> dict[str, Any]:
    """Tiny helper: win-rate by ML score floor bucket (for overnight report)."""
    p = path or outcomes_path()
    if not p.is_file():
        return {"buckets": {}, "n": 0}
    rows: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {"buckets": {}, "n": 0}
    rows = rows[-max_rows:]
    stats: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "wins": 0})
    for row in rows:
        score = row.get("ml_score")
        pnl = row.get("pnl")
        if score is None or pnl is None:
            continue
        try:
            s = float(score)
            pval = float(pnl)
        except (TypeError, ValueError):
            continue
        label = "lt_0.50"
        for floor in buckets:
            if s >= float(floor):
                label = f"ge_{floor:.2f}"
        bucket = stats[label]
        bucket["n"] += 1
        if pval > 0:
            bucket["wins"] += 1
    out_buckets: dict[str, Any] = {}
    for key, st in sorted(stats.items()):
        n = int(st["n"])
        wins = int(st["wins"])
        out_buckets[key] = {
            "n": n,
            "wins": wins,
            "wr": round(wins / n, 4) if n else None,
        }
    return {"buckets": out_buckets, "n": len(rows)}
