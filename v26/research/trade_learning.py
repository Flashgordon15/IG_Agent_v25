"""
Unified trade learning — live fills, ML training store, and OHLC replay labels.

v26 learns from three grounded outcome sources:
  1. feeder fill_close (live IG-confirmed P&L)
  2. ml_training_store.jsonl (v25 feature-rich closed trades)
  3. replay_results.jsonl (historical signal replay with 3/6-bar labels)

Offline model training (S4_ml_meta) is not wired yet; this module produces the
labelled dataset summary and readiness gates for ml_veto promotion.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from expectancy.engine import collect_fills, portfolio_summary
from research.walk_forward import load_replay_rows


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ml_store_path() -> Path:
    return _project_root() / "src" / "data" / "ml_training_store.jsonl"


def _load_v26_config() -> dict[str, Any]:
    path = _project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _label(row: dict[str, Any]) -> str:
    from research.label_utils import outcome_label

    return outcome_label(row)


def _wr_stats(
    rows: list[dict[str, Any]],
    *,
    label_key: str = "label",
    pnl_key: str | None = None,
) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "wins": 0, "losses": 0, "wr": 0.0, "e_gbp": 0.0}
    wins = losses = breakeven = 0
    pnls: list[float] = []
    for row in rows:
        if pnl_key:
            pnl = float(row.get(pnl_key) or 0)
            pnls.append(pnl)
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            else:
                breakeven += 1
        else:
            lab = str(row.get(label_key) or _label(row))
            if lab == "WIN":
                wins += 1
            elif lab == "LOSS":
                losses += 1
            else:
                breakeven += 1
    n = len(rows)
    decided = wins + losses
    wr = wins / decided if decided else 0.0
    e_gbp = sum(pnls) / n if pnls else 0.0
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "decided": decided,
        "wr": round(wr, 4),
        "e_gbp": round(e_gbp, 2),
        "breakeven_pct": round(breakeven / n, 4) if n else 0.0,
    }


def load_ml_training_records() -> list[dict[str, Any]]:
    path = _ml_store_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            continue
    return rows


def summarize_live_fills(*, days: int = 90) -> dict[str, Any]:
    fills = collect_fills(days=days)
    by_epic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_setup: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in fills:
        by_epic[str(f.get("epic") or "unknown")].append(f)
        by_setup[str(f.get("setup_key") or "unknown")].append(f)

    setup_rows = [
        {
            "setup_key": sk,
            **_wr_stats(rows, pnl_key="pnl_gbp"),
        }
        for sk, rows in sorted(by_setup.items(), key=lambda x: -len(x[1]))
    ]
    epic_rows = [
        {
            "epic": epic,
            **_wr_stats(rows, pnl_key="pnl_gbp"),
        }
        for epic, rows in sorted(by_epic.items(), key=lambda x: -len(x[1]))
    ]
    return {
        "source": "feeder_fill_close",
        "rolling_days": days,
        "portfolio": portfolio_summary(fills),
        "by_setup": setup_rows[:20],
        "by_epic": epic_rows,
    }


def summarize_ml_store() -> dict[str, Any]:
    records = load_ml_training_records()
    by_instrument: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        by_instrument[str(row.get("instrument") or "unknown")].append(row)

    labelled = [
        r
        for r in records
        if str(r.get("result") or "").upper() in ("WIN", "LOSS")
        or float(r.get("gbp_pnl") or 0) != 0
    ]
    return {
        "source": "ml_training_store",
        "path": str(_ml_store_path()),
        "total_records": len(records),
        "labelled_records": len(labelled),
        "portfolio": _wr_stats(labelled, pnl_key="gbp_pnl"),
        "feature_columns": [
            "confidence",
            "rsi",
            "atr",
            "spread",
            "volume_regime",
            "session_window",
            "fitness_score",
            "points_state",
        ],
        "by_instrument": [
            {
                "instrument": inst,
                **_wr_stats(rows, pnl_key="gbp_pnl"),
            }
            for inst, rows in sorted(by_instrument.items(), key=lambda x: -len(x[1]))
        ],
    }


def summarize_replay_historical(
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = rows if rows is not None else load_replay_rows()
    fired = [r for r in data if r.get("fired")]
    by_epic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_setup: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in data:
        epic = str(row.get("epic") or "unknown")
        setup = str(row.get("setup_key") or "unknown")
        by_epic[epic].append(row)
        by_setup[setup].append(row)

    def _epic_summary(epic: str, epic_rows: list[dict[str, Any]]) -> dict[str, Any]:
        fired_rows = [r for r in epic_rows if r.get("fired")]
        return {
            "epic": epic,
            "all_signals": _wr_stats(epic_rows),
            "fired_only": _wr_stats(fired_rows),
        }

    return {
        "source": "replay_results_jsonl",
        "path": str(_project_root() / "src" / "data" / "replay_results.jsonl"),
        "total_rows": len(data),
        "fired_rows": len(fired),
        "portfolio": _wr_stats(data),
        "fired_portfolio": _wr_stats(fired),
        "by_epic": [
            _epic_summary(epic, epic_rows)
            for epic, epic_rows in sorted(by_epic.items(), key=lambda x: -len(x[1]))
        ],
        "top_setups_fired": [
            {
                "setup_key": sk,
                **_wr_stats([r for r in rows if r.get("fired")]),
            }
            for sk, rows in sorted(by_setup.items(), key=lambda x: -len(x[1]))[:15]
            if any(r.get("fired") for r in rows)
        ],
    }


def ml_readiness(
    *,
    live: dict[str, Any],
    ml_store: dict[str, Any],
    replay: dict[str, Any],
) -> dict[str, Any]:
    cfg = _load_v26_config().get("ml_veto") or {}
    min_rows = int(cfg.get("min_labelled_rows") or 500)
    live_n = int((live.get("portfolio") or {}).get("n") or 0)
    ml_n = int(ml_store.get("labelled_records") or 0)
    replay_decided = int((replay.get("fired_portfolio") or {}).get("decided") or 0)
    combined_proxy = live_n + ml_n + replay_decided
    return {
        "min_labelled_rows": min_rows,
        "live_fills": live_n,
        "ml_training_records": ml_n,
        "replay_fired_decided": replay_decided,
        "combined_proxy": combined_proxy,
        "ready_for_ml_veto": combined_proxy >= min_rows,
        "ml_veto_enabled_in_config": bool(cfg.get("enabled")),
        "note": (
            "ml_veto uses offline labels; replay fired+decided counts toward proxy "
            "until S4 retrain pipeline ships."
        ),
    }


def learning_tips(
    *,
    live: dict[str, Any],
    ml_store: dict[str, Any],
    replay: dict[str, Any],
    readiness: dict[str, Any],
) -> list[str]:
    tips: list[str] = []
    live_n = int((live.get("portfolio") or {}).get("n") or 0)
    replay_n = int(replay.get("total_rows") or 0)
    ml_n = int(ml_store.get("total_records") or 0)

    if replay_n > 1000:
        fired = replay.get("fired_portfolio") or {}
        tips.append(
            f"Historic replay: {replay_n:,} labelled signals "
            f"({fired.get('decided', 0):,} decided on fired) — primary ML training pool."
        )
    if ml_n > 0:
        tips.append(
            f"ML store: {ml_n} confirmed closes with full feature vectors (v25 XGBoost path)."
        )
    if live_n > 0:
        wr = (live.get("portfolio") or {}).get("wr", 0)
        tips.append(
            f"Live fills: {live_n} closes in feeder — ground truth WR {wr:.0%}."
        )
    elif live_n == 0:
        tips.append(
            "No live fills yet — historic replay + shadow counterfactuals drive learning."
        )
    if not readiness.get("ready_for_ml_veto"):
        need = int(readiness.get("min_labelled_rows") or 500)
        have = int(readiness.get("combined_proxy") or 0)
        tips.append(
            f"ml_veto gate: {have}/{need} labelled rows (proxy) — keep disabled."
        )
    return tips


def _load_s4_manifest() -> dict[str, Any]:
    path = _project_root() / "data_lake" / "models" / "s4" / "manifest.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def build_trade_learning_report(*, live_days: int = 90) -> dict[str, Any]:
    live = summarize_live_fills(days=live_days)
    ml_store = summarize_ml_store()
    replay = summarize_replay_historical()
    readiness = ml_readiness(live=live, ml_store=ml_store, replay=replay)
    s4 = _load_s4_manifest()
    s4_wired = bool(s4.get("by_epic"))
    tips = learning_tips(
        live=live,
        ml_store=ml_store,
        replay=replay,
        readiness=readiness,
    )
    if s4_wired:
        tips.insert(
            0,
            f"S4 models v{s4.get('version')}: {s4.get('epics_veto_eligible', 0)} "
            f"epics veto-eligible (ml_veto still off until you enable).",
        )
    return {
        "ok": True,
        "live_fills": live,
        "ml_training_store": ml_store,
        "replay_historical": replay,
        "ml_readiness": readiness,
        "s4_manifest": s4,
        "learning_tips": tips,
        "s4_ml_meta_status": "wired" if s4_wired else "pending_retrain",
        "s4_note": (
            "Run scripts/v26_s4_retrain.py after OHLC replay to refresh per-epic models."
        ),
    }


def write_trade_learning_snapshot(*, live_days: int = 90) -> Path:
    payload = build_trade_learning_report(live_days=live_days)
    out_dir = _project_root() / "data_lake" / "state"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "v26_trade_learning.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
