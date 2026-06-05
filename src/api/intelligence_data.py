"""Intelligence tab data — replay, shadow, learning."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from system.paths import data_dir


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def replay_summary() -> dict[str, Any]:
    analysis_path = data_dir() / "replay_analysis.txt"
    results_path = data_dir() / "replay_results.jsonl"
    cache_path = data_dir() / "ohlc_cache" / "nikkei_5m.jsonl"
    rows = _read_jsonl(results_path)
    dates: list[str] = []
    for r in rows:
        ts = str(r.get("timestamp") or r.get("bar_time") or "")
        if len(ts) >= 10:
            dates.append(ts[:10])
    best_threshold = None
    best_rsi = None
    recommendations: list[str] = []
    if analysis_path.is_file():
        text = analysis_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "Best threshold" in line or "best threshold" in line.lower():
                best_threshold = line.strip()
            if "Best RSI" in line or "rsi range" in line.lower():
                best_rsi = line.strip()
            if line.strip().startswith("→") or "Recommend" in line:
                recommendations.append(line.strip())
    bars = (
        sum(1 for _ in cache_path.read_text(encoding="utf-8").splitlines() if _.strip())
        if cache_path.is_file()
        else 0
    )
    mtime = analysis_path.stat().st_mtime if analysis_path.is_file() else None
    return {
        "bars_analysed": len(rows),
        "bars_cache": bars,
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
        "best_threshold": best_threshold,
        "best_rsi": best_rsi,
        "recommendations": recommendations[:8],
        "last_updated": (
            datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
            if mtime
            else None
        ),
    }


def _tail_jsonl_today(
    path: Path, today: str, *, tail_bytes: int = 2 * 1024 * 1024
) -> list[dict[str, Any]]:
    """Read only today's records from a large JSONL by tailing the file.

    Shadow logs grow indefinitely. Reading the full file on every request is O(40MB+).
    Since entries are time-ordered we only need the tail where today's records live.
    """
    if not path.is_file():
        return []
    size = path.stat().st_size
    seek_pos = max(0, size - tail_bytes)
    rows: list[dict[str, Any]] = []
    with open(path, "rb") as f:
        f.seek(seek_pos)
        if seek_pos > 0:
            f.readline()  # skip partial first line
        for line in f:
            try:
                obj = json.loads(line)
                if str(obj.get("timestamp", "")).startswith(today):
                    rows.append(obj)
            except json.JSONDecodeError:
                continue
    return rows


def shadow_today() -> dict[str, Any]:
    path = data_dir() / "shadow_log.jsonl"
    today = date.today().isoformat()
    rows = _tail_jsonl_today(path, today)
    would = sum(1 for r in rows if r.get("would_have_fired"))
    # Only count rows that had a real signal — skip "collecting" phase (empty setup_key)
    blocked = Counter(
        str(r.get("setup_key"))
        for r in rows
        if not r.get("would_have_fired") and r.get("setup_key")
    )
    top_block = blocked.most_common(1)[0][0] if blocked else None
    top_3 = [{"setup_key": sk, "count": cnt} for sk, cnt in blocked.most_common(3)]
    # Count rows where confidence was within 5 pts of a standard 80% threshold
    lower_thresh = sum(
        1
        for r in rows
        if not r.get("would_have_fired")
        and r.get("setup_key")
        and float(r.get("adjusted_score", 0)) >= 75.0
    )
    return {
        "evaluations": len(rows),
        "would_have_traded": would,
        "top_blocked_setup": top_block,
        "top_3_setups": top_3,
        "estimated_extra_if_threshold_minus_5": lower_thresh,
    }


def learning_status() -> dict[str, Any]:
    ml_path = data_dir() / "ml_training_store.jsonl"
    ml_count = len(_read_jsonl(ml_path))
    confirmed = 0
    top_setups: list[dict[str, Any]] = []
    try:
        from data.learning_store import LearningStore
        from system.config_loader import ConfigLoader
        from system.paths import config_dir

        cfg = ConfigLoader(config_dir() / "config_v25.json").load_config()
        store = LearningStore(str(cfg.learning_db))
        if hasattr(store, "recent_confirmed_closed_trades"):
            confirmed = len(store.recent_confirmed_closed_trades(limit=500))
        rows = store.conn.execute(
            """
            SELECT setup_key, COUNT(*) AS n FROM trades
            WHERE closed_at IS NOT NULL AND setup_key IS NOT NULL
            GROUP BY setup_key ORDER BY n DESC LIMIT 5
            """
        ).fetchall()
        top_setups = [{"setup_key": r[0], "count": int(r[1])} for r in rows]
    except Exception:
        pass
    target = 500
    return {
        "ml_records": ml_count,
        "confirmed_trades": confirmed,
        "top_setups": top_setups,
        "ml_viability_target": target,
        "ml_viability_pct": min(100, int(100 * ml_count / target)) if target else 0,
    }


def run_replay_pipeline() -> dict[str, Any]:
    from system.replay_scheduler_runner import in_replay_api_window
    from system.replay_scheduler_runner import run_replay_pipeline as _run

    if not in_replay_api_window():
        return {
            "ok": False,
            "error": "Replay blocked during live window 22:30–07:00 BST",
        }
    rc = _run(scheduled=False)
    return {"ok": rc == 0, "returncode": rc}
