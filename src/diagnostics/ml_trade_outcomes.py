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
from datetime import datetime, timezone
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


def resolve_ml_score_and_source(
    *,
    ml_score: float | None = None,
    deal_id: str = "",
    epic: str = "",
) -> tuple[float | None, str]:
    """Recover the entry score and record where it came from.

    The per-epic sniper snapshot is shared by every deal on that epic, so a
    score recovered from it is tagged as a fallback rather than a per-trade
    inference. See ``diagnostics.stamp_provenance``.
    """
    from diagnostics.stamp_provenance import (
        ML_SOURCE_ABSENT,
        ML_SOURCE_EPIC_SNAPSHOT,
        ML_SOURCE_EXECUTION_PARAMS,
        ML_SOURCE_MODEL,
    )

    if ml_score is not None:
        try:
            return float(ml_score), ML_SOURCE_EXECUTION_PARAMS
        except (TypeError, ValueError):
            pass
    epic_s = str(epic or "").strip()
    deal = str(deal_id or "").strip()
    # Deal-keyed fill stamp (survives sniper snapshot rotation).
    if deal:
        try:
            from data.ml_training_store import peek_buffered_entry

            buffered = peek_buffered_entry(deal)
            if isinstance(buffered, dict):
                buffered_source = str(buffered.get("ml_score_source") or "").strip()
                for key in ("ml_score_at_entry", "p_success", "ml_score"):
                    if buffered.get(key) is not None:
                        return (
                            float(buffered[key]),
                            buffered_source or ML_SOURCE_MODEL,
                        )
        except Exception:
            pass
    # Autopsy / learning store row written at entry.
    if deal:
        try:
            from data.ml_training_store import deal_id_aliases
            from system.paths import data_dir

            autopsy_dir = Path(data_dir()) / "autopsy"
            for alias in deal_id_aliases(deal):
                autopsy = autopsy_dir / f"{alias}.json"
                if not autopsy.is_file():
                    continue
                raw = json.loads(autopsy.read_text(encoding="utf-8"))
                raw_source = str(raw.get("ml_score_source") or "").strip()
                for key in (
                    "ml_score_at_entry",
                    "ml_score",
                    "p_success",
                    "confidence_at_entry",
                ):
                    if raw.get(key) is not None:
                        return float(raw[key]), raw_source or ML_SOURCE_MODEL
        except Exception:
            pass
    # Live sniper last-by-epic — shared across deals, so fallback-tagged.
    # Never promote a gate threshold constant (e.g. 0.68) as if it were an
    # inference — that polluted 2026-07-24 journal stamps.
    try:
        from alpha.micro_sniper_ml import latest_sniper_ml_snapshot
        from diagnostics.stamp_provenance import is_threshold_constant

        snap = latest_sniper_ml_snapshot(epic=epic_s or None)
        if isinstance(snap, dict) and snap.get("p_success") is not None:
            p = float(snap["p_success"])
            # Refuse classic sniper default; also refuse other known thr constants
            # from shared epic cache (not per-deal inference).
            if is_threshold_constant(p):
                return None, ML_SOURCE_ABSENT
            return p, ML_SOURCE_EPIC_SNAPSHOT
    except Exception:
        pass
    return None, ML_SOURCE_ABSENT


def resolve_ml_score_for_close(
    *,
    ml_score: float | None = None,
    deal_id: str = "",
    epic: str = "",
) -> float | None:
    """Prefer explicit score; else recover from entry buffer / autopsy / sniper snap."""
    score, _source = resolve_ml_score_and_source(
        ml_score=ml_score, deal_id=deal_id, epic=epic
    )
    return score


def _timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def resolve_hold_sec_for_close(
    *,
    hold_sec: float | None = None,
    deal_id: str = "",
    closed_at_ts: float | None = None,
) -> float | None:
    """Recover hold duration from deal-keyed entry evidence or learning DB."""
    if hold_sec is not None:
        try:
            hold = float(hold_sec)
            return hold if 0.0 <= hold <= 7 * 24 * 3600 else None
        except (TypeError, ValueError):
            pass
    deal = str(deal_id or "").strip()
    close_ts = _timestamp(closed_at_ts)
    if deal and close_ts is not None:
        try:
            from data.ml_training_store import peek_buffered_entry

            buffered = peek_buffered_entry(deal)
            if isinstance(buffered, dict):
                entry_ts = _timestamp(
                    buffered.get("entry_time")
                    or buffered.get("opened_at")
                    or buffered.get("timestamp")
                )
                if entry_ts is not None:
                    hold = close_ts - entry_ts
                    if 0.0 <= hold <= 7 * 24 * 3600:
                        return hold
        except Exception:
            pass
    if deal:
        try:
            from data.learning_store import hold_sec_from_learning_deal

            return hold_sec_from_learning_deal(deal)
        except Exception:
            pass
    return None


def resolve_regime_for_close(
    *,
    regime: str | None = None,
    deal_id: str = "",
    epic: str = "",
) -> str:
    """Prefer explicit regime; else recover from entry buffer / live snapshot."""
    explicit = str(regime or "").strip()
    if explicit:
        return explicit
    deal = str(deal_id or "").strip()
    if deal:
        try:
            from data.ml_training_store import peek_buffered_entry

            buffered = peek_buffered_entry(deal)
            if isinstance(buffered, dict):
                for key in ("market_regime", "regime"):
                    val = str(buffered.get(key) or "").strip()
                    if val:
                        return val
        except Exception:
            pass
    try:
        from system.regime_state import get_regime_state_snapshot

        snap = get_regime_state_snapshot() or {}
        label = str(
            snap.get("regime") or snap.get("market_regime") or snap.get("label") or ""
        ).strip()
        if label:
            return label
        epic_s = str(epic or "").strip()
        if epic_s:
            by_epic = snap.get("by_epic") or snap.get("markets") or {}
            if isinstance(by_epic, dict):
                row = by_epic.get(epic_s) or {}
                if isinstance(row, dict):
                    return str(row.get("regime") or row.get("label") or "").strip()
    except Exception:
        pass
    # Learning DB notes / extras often carry regime when buffer was never written.
    if deal:
        try:
            from data.learning_store import LearningStore
            from system.paths import data_dir

            store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
            row = store.conn.execute(
                "SELECT notes FROM trades WHERE ig_deal_id=? OR deal_reference=? "
                "ORDER BY id DESC LIMIT 1",
                (deal, deal),
            ).fetchone()
            if row is not None:
                notes = str(row["notes"] or "")
                for token in ("market_regime=", "regime="):
                    if token in notes:
                        part = notes.split(token, 1)[1].split()[0].strip(",;")
                        if part and part.upper() not in {"UNKNOWN", "NONE", "NULL"}:
                            return part
        except Exception:
            pass
    return ""


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
    hold_sec_source: str = "",
    exit_authority: str = "",
    path: Path | None = None,
) -> bool:
    """Append one structured outcome. Idempotent on deal_id within process."""
    deal = str(deal_id or "").strip()
    if deal:
        with _LOCK:
            if deal in _SEEN:
                return False
            _SEEN.add(deal)
    resolved, resolved_source = resolve_ml_score_and_source(
        ml_score=ml_score, deal_id=deal, epic=str(epic or "")
    )
    from diagnostics.stamp_provenance import (
        classify_exit_authority,
        classify_hold,
        classify_ml_score,
    )

    ml_stamp = classify_ml_score(resolved, source=resolved_source)
    hold_stamp = classify_hold(
        hold_sec,
        exit_reason=str(exit_reason or ""),
        engine_origin=str(engine_origin or ""),
        source=hold_sec_source or "",
    )
    authority = exit_authority or classify_exit_authority(
        exit_reason=str(exit_reason or ""),
        engine_origin=str(engine_origin or ""),
    )
    row: dict[str, Any] = {
        "ts": time.time(),
        "account": str(account_id or ""),
        "epic": str(epic or ""),
        "side": str(side or "").upper(),
        "ml_score": ml_stamp["ml_score_at_entry"],
        "ml_score_at_entry": ml_stamp["ml_score_at_entry"],
        "ml_score_source": ml_stamp["ml_score_source"],
        "ml_score_trusted": ml_stamp["ml_score_trusted"],
        "regime": str(regime or ""),
        "market_regime": str(regime or ""),
        "style": str(style or ""),
        "pnl": None if pnl is None else round(float(pnl), 4),
        "deal_id": deal,
        "exit_reason": str(exit_reason or "")[:160],
        "hold_sec": hold_stamp["hold_sec"],
        "hold_duration_seconds": hold_stamp["hold_sec"],
        "hold_sec_source": hold_stamp["hold_sec_source"],
        "hold_sec_trusted": hold_stamp["hold_sec_trusted"],
        "exit_authority": authority,
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
