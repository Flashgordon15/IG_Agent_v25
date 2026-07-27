#!/usr/bin/env python3
"""Offline dual-engine ML learning harness — sniper (CFD micro) + long (SB macro/LTR).

Fully offline / shadow-isolated. Never contacts the broker, never opens a port,
never touches the live trading engines, and never writes into live setup_stats /
expectancy. Safe to run while markets are shut.

Pipeline (all steps are reversible and idempotent):

  inventory  Read-only counts of real learning rows per style/epic across
             ml_training_store.jsonl, ml_trade_outcomes.jsonl, shadow registry.
  backfill   Stamp style/epic (+ ml_score_at_entry / hold_sec where derivable)
             onto existing ml_training_store.jsonl rows by joining deal id ->
             ml_trade_outcomes / learning_db. Non-derivable style => "unknown".
  build      Forward-label Gold/DOW/Nikkei from local OHLC caches (3-bar sniper /
             6-bar long) and stamp the widened microstructure feature vector.
  replay     Run REAL market replay data (replay_results.jsonl) through BOTH
             decision horizons — sniper=3-bar micro exit, long=6-bar macro exit —
             writing labeled outcomes to the isolated shadow_replay_outcomes plane.
  train      Refresh model on widened replay labels (xgboost when available) +
             gated live-store train; report per-style AUC/Brier. Never claims an
             improvement epoch under NOT_MEASURABLE.

Usage::

  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \
    PYTHONPATH=src .venv/bin/python3 scripts/ml_replay_learn.py all
  ... scripts/ml_replay_learn.py inventory
  ... scripts/ml_replay_learn.py backfill [--apply]
  ... scripts/ml_replay_learn.py build
  ... scripts/ml_replay_learn.py train
  ... scripts/ml_replay_learn.py replay [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data.ml_training_store import deal_id_aliases, default_store_path
from ml.replay_features import FEATURE_NAMES, attach_features_to_records, features_from_replay_row
from ml.style_epic import LONG, SNIPER, UNKNOWN, epic_for_instrument, resolve_ml_style
from system.paths import data_dir, project_root

# Source of REAL market replay data (scored setups + forward price labels).
_REPLAY_SOURCES = (
    data_dir() / "replay_results.jsonl",
    project_root() / "src" / "data" / "replay_results.jsonl",
)

# Dual-desk epics with local OHLC caches (Gold + DOW hot-path + Nikkei).
_BUILD_EPICS: tuple[tuple[str, str], ...] = (
    ("CS.D.CFPGOLD.CFP.IP", "Spot Gold"),
    ("IX.D.DOW.IFM.IP", "Wall Street"),
    ("IX.D.NIKKEI.IFM.IP", "Japan 225"),
)


def _learning_db_path() -> Path:
    return data_dir() / "learning_db.sqlite3"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _ml_outcomes_index() -> dict[str, dict[str, Any]]:
    """ml_trade_outcomes rows indexed by every deal-id alias."""
    rows = _read_jsonl(data_dir() / "metrics" / "ml_trade_outcomes.jsonl")
    idx: dict[str, dict[str, Any]] = {}
    for r in rows:
        did = str(r.get("deal_id") or "")
        if not did:
            continue
        for alias in [did] + deal_id_aliases(did):
            idx.setdefault(alias, r)
    return idx


# ---------------------------------------------------------------------------
# Inventory (read-only)
# ---------------------------------------------------------------------------
def inventory() -> dict[str, Any]:
    from data.learning_store import LearningStore
    from data.shadow_training_registry import (
        ensure_replay_schema,
        replay_style_epic_counts,
    )

    ml_rows = _read_jsonl(default_store_path())
    out_rows = _read_jsonl(data_dir() / "metrics" / "ml_trade_outcomes.jsonl")

    def _style_epic_summary(rows: list[dict[str, Any]], *, style_key: str) -> dict[str, Any]:
        styles: Counter[str] = Counter()
        epics: Counter[str] = Counter()
        hold = 0
        mlscore = 0
        wl = 0
        for r in rows:
            styles[str(r.get(style_key) or r.get("style") or UNKNOWN)] += 1
            epics[str(r.get("epic") or r.get("instrument") or "?")] += 1
            if r.get("hold_sec") is not None or r.get("HoldSec") is not None:
                hold += 1
            if (
                r.get("ml_score_at_entry") is not None
                or r.get("ml_score") is not None
                or r.get("p_success") is not None
            ):
                mlscore += 1
            if str(r.get("result") or "").upper() in ("WIN", "LOSS"):
                wl += 1
        return {
            "rows": len(rows),
            "win_loss_labels": wl,
            "style": dict(styles),
            "epic_top": dict(epics.most_common(12)),
            "hold_stamped": hold,
            "mlscore_stamped": mlscore,
        }

    db = LearningStore(str(_learning_db_path()))
    cur = db.conn.cursor()
    ensure_replay_schema(cur)
    shadow_reg = cur.execute(
        "SELECT COALESCE(NULLIF(TRIM(epic),''),'?') e, COUNT(*) c "
        "FROM shadow_training_registry WHERE is_shadow=1 GROUP BY e ORDER BY c DESC"
    ).fetchall()

    report = {
        "ml_training_store": _style_epic_summary(ml_rows, style_key="style"),
        "ml_trade_outcomes": _style_epic_summary(out_rows, style_key="style"),
        "shadow_training_registry": {
            "total": sum(int(r[1]) for r in shadow_reg),
            "by_epic": {str(r[0]): int(r[1]) for r in shadow_reg},
        },
        "shadow_replay_outcomes": replay_style_epic_counts(db.conn),
    }
    return report


def _print_inventory(report: dict[str, Any]) -> None:
    print("=== INVENTORY (real learning rows) ===")
    for name, block in report.items():
        print(f"\n[{name}]")
        print(json.dumps(block, indent=2, default=str))


# ---------------------------------------------------------------------------
# Backfill live ml_training_store style/epic (+ ml_score/hold where derivable)
# ---------------------------------------------------------------------------
def backfill_ml_training_store(*, apply: bool) -> dict[str, Any]:
    from data.learning_store import LearningStore, _hold_sec_between

    path = default_store_path()
    rows = _read_jsonl(path)
    if not rows:
        return {"rows": 0, "note": "no ml_training_store rows"}

    out_idx = _ml_outcomes_index()
    db = LearningStore(str(_learning_db_path()))
    cur = db.conn.cursor()

    stats = {
        "rows": len(rows),
        "epic_stamped": 0,
        "style_stamped": 0,
        "style_unknown": 0,
        "ml_score_stamped": 0,
        "hold_stamped": 0,
        "style_source": Counter(),
    }
    updated: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        did = str(r.get("deal_id") or "")
        aliases = [did] + deal_id_aliases(did)

        # --- learning DB join (authoritative epic / engine_origin / hold) ---
        db_row = None
        for a in aliases:
            db_row = cur.execute(
                "SELECT epic, engine_origin, opened_at, closed_at "
                "FROM trades WHERE ig_deal_id=? OR deal_reference=? "
                "ORDER BY id DESC LIMIT 1",
                (a, a),
            ).fetchone()
            if db_row:
                break

        # --- ml_trade_outcomes join (style / ml_score / hold) ---
        oc = None
        for a in aliases:
            if a in out_idx:
                oc = out_idx[a]
                break

        # epic: prefer explicit -> learning DB -> instrument mapping
        db_epic = str(db_row["epic"]) if db_row and db_row["epic"] else ""
        epic = epic_for_instrument(
            r.get("instrument"),
            fallback_epic=r.get("epic") or db_epic,
        )
        if epic:
            r["epic"] = epic
            stats["epic_stamped"] += 1

        # hold_sec: outcomes -> learning DB opened/closed
        hold = None
        if oc is not None and oc.get("hold_sec") is not None:
            hold = oc.get("hold_sec")
        elif db_row is not None:
            hold = _hold_sec_between(db_row["opened_at"], db_row["closed_at"])
        if hold is not None and r.get("hold_sec") is None:
            r["hold_sec"] = round(float(hold), 1)
        if r.get("hold_sec") is not None:
            stats["hold_stamped"] += 1

        # ml_score_at_entry: from outcomes ml_score
        if r.get("ml_score_at_entry") is None and oc is not None:
            ms = oc.get("ml_score")
            if ms is not None:
                try:
                    r["ml_score_at_entry"] = round(float(ms), 6)
                    r.setdefault("p_success", r["ml_score_at_entry"])
                except (TypeError, ValueError):
                    pass
        if r.get("ml_score_at_entry") is not None:
            stats["ml_score_stamped"] += 1

        # style: outcomes hint -> engine origin -> exit reason -> hold split
        style_hint = oc.get("style") if oc is not None else None
        engine_origin = (
            str(db_row["engine_origin"]) if db_row and db_row["engine_origin"] else ""
        )
        style = resolve_ml_style(
            style_hint=style_hint,
            engine_origin=engine_origin,
            exit_reason=r.get("exit_reason"),
            hold_sec=r.get("hold_sec"),
        )
        r["style"] = style
        if style == UNKNOWN:
            stats["style_unknown"] += 1
        else:
            stats["style_stamped"] += 1
        if style_hint:
            stats["style_source"]["ml_trade_outcomes"] += 1
        elif engine_origin:
            stats["style_source"]["engine_origin"] += 1
        elif r.get("hold_sec") is not None:
            stats["style_source"]["hold_split"] += 1
        else:
            stats["style_source"]["none"] += 1
        updated.append(r)

    stats["style_source"] = dict(stats["style_source"])
    stats["style_dist"] = dict(Counter(r.get("style") for r in updated))

    if apply:
        backup = path.with_suffix(
            f".jsonl.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        shutil.copy2(path, backup)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for r in updated:
                fh.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
        tmp.replace(path)
        stats["backup"] = str(backup)
        stats["applied"] = True
    else:
        stats["applied"] = False
    return stats


# ---------------------------------------------------------------------------
# Build multi-epic forward-labeled replay + widened features from OHLC caches
# ---------------------------------------------------------------------------
def build_multi_epic_replay() -> dict[str, Any]:
    """Forward-label Gold/DOW/Nikkei from local OHLC and stamp widened features."""
    import importlib.util

    replay_path = project_root() / "scripts" / "replay_signals.py"
    spec = importlib.util.spec_from_file_location("replay_signals_mod", replay_path)
    if spec is None or spec.loader is None:
        return {"ok": False, "reason": "cannot_load_replay_signals"}
    mod = importlib.util.module_from_spec(spec)
    # Ensure scripts/ imports resolve if replay_signals pulls siblings.
    sys.path.insert(0, str(project_root() / "src"))
    spec.loader.exec_module(mod)

    from system.config_loader import ConfigLoader
    from trading.instrument_registry import InstrumentRegistry
    from trading.ohlc_cache_paths import ohlc_cache_path

    cfg_path = project_root() / "config" / "config_v25.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    reg = InstrumentRegistry(raw)
    base_cfg = ConfigLoader(cfg_path).load_config()

    out_paths = [
        data_dir() / "replay_results.jsonl",
        project_root() / "src" / "data" / "replay_results.jsonl",
    ]
    for p in out_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")

    per_epic: dict[str, Any] = {}
    total_records = 0
    total_fired = 0
    label_counts: dict[str, Counter] = {
        SNIPER: Counter(),
        LONG: Counter(),
    }

    for epic, market in _BUILD_EPICS:
        inst = reg.get_by_epic(epic) or {"epic": epic, "name": market}
        cache_path = ohlc_cache_path(epic, market=market)
        # Fall back to legacy src/data/ohlc_cache if production cache empty/missing.
        bars = mod._load_bars(cache_path)
        if not bars:
            legacy = project_root() / "src" / "data" / "ohlc_cache" / cache_path.name
            bars = mod._load_bars(legacy)
            cache_path = legacy if bars else cache_path
        cfg = mod._config_for_instrument(base_cfg, inst)
        stop_pts = float(getattr(cfg, "stop_distance_points", 45) or 45)
        threshold = float(cfg.signal_threshold)
        if not bars:
            per_epic[epic] = {
                "ok": False,
                "reason": "no_ohlc_bars",
                "cache": str(cache_path),
            }
            continue
        records, summary = mod._replay_batch(
            epic=epic,
            market=market,
            bars=bars,
            cfg=cfg,
            stop_pts=stop_pts,
            threshold=threshold,
        )
        # Ensure stop_pts / session_window present for feature extraction.
        for r in records:
            r.setdefault("stop_pts", stop_pts)
            if not r.get("session_window") and r.get("timestamp"):
                try:
                    from signals.indicators import session_name

                    dt = datetime.fromisoformat(
                        str(r["timestamp"]).replace("Z", "+00:00")
                    )
                    r["session_window"] = session_name(dt)
                except Exception:
                    pass
        enriched = attach_features_to_records(records, bars)
        for r in enriched:
            for style, key in ((SNIPER, "label_3bar"), (LONG, "label_6bar")):
                lab = str(r.get(key) or "").upper()
                if lab in ("WIN", "LOSS", "BREAKEVEN"):
                    label_counts[style][lab] += 1
            # Only count fired rows toward "usable" fired totals.
            if bool(r.get("fired")):
                total_fired += 1

        for path in out_paths:
            with path.open("a", encoding="utf-8") as fh:
                for r in enriched:
                    fh.write(json.dumps(r, default=str) + "\n")

        total_records += len(enriched)
        per_epic[epic] = {
            "ok": True,
            "market": market,
            "cache": str(cache_path),
            "bars": len(bars),
            "records": len(enriched),
            "fired": int(summary.get("fired") or 0),
            "labels_3bar": dict(summary.get("labels_3") or {}),
            "feature_names": list(FEATURE_NAMES),
        }

    return {
        "ok": True,
        "outputs": [str(p) for p in out_paths],
        "total_records": total_records,
        "total_fired_rows_written": total_fired,
        "label_counts_all_rows": {k: dict(v) for k, v in label_counts.items()},
        "per_epic": per_epic,
        "feature_names": list(FEATURE_NAMES),
        "feature_count": len(FEATURE_NAMES),
    }


# ---------------------------------------------------------------------------
# Dual-engine replay over REAL market data (sniper 3-bar / long 6-bar)
# ---------------------------------------------------------------------------
# Each engine is a distinct exit horizon on the same real forward price path:
#   sniper -> label_3bar (fast micro exit)   long -> label_6bar (macro hold >= LTR arm)
_ENGINES = (
    {"style": SNIPER, "engine": "cfd_micro_sniper", "label": "label_3bar", "horizon": 3},
    {"style": LONG, "engine": "sb_macro_ltr", "label": "label_6bar", "horizon": 6},
)


def _bar_interval_seconds(rows: list[dict[str, Any]]) -> float:
    """Infer replay bar spacing from the first two parseable timestamps."""
    times: list[datetime] = []
    for r in rows[:50]:
        ts = str(r.get("timestamp") or "")
        try:
            times.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        except ValueError:
            continue
        if len(times) >= 2:
            break
    if len(times) >= 2:
        delta = abs((times[1] - times[0]).total_seconds())
        if delta > 0:
            return delta
    return 300.0  # default 5m bars


def replay_both_engines(*, limit: int | None = None) -> dict[str, Any]:
    import warnings

    from data.learning_store import LearningStore
    from data.shadow_training_registry import (
        ensure_replay_schema,
        replay_style_epic_counts,
        upsert_shadow_replay,
    )
    from trading.ml_scorer import get_ml_scorer

    src = next((p for p in _REPLAY_SOURCES if p.is_file()), None)
    if src is None:
        return {"ok": False, "reason": "no replay_results.jsonl found", "written": 0}

    rows = _read_jsonl(src)
    if limit:
        rows = rows[:limit]
    bar_sec = _bar_interval_seconds(rows)

    scorer = get_ml_scorer()
    db = LearningStore(str(_learning_db_path()))
    # Full deterministic rebuild from the source file — clear prior replay rows so
    # the isolated plane stays idempotent (never accumulates orphans across runs).
    cur = db.conn.cursor()
    ensure_replay_schema(cur)
    cur.execute("DELETE FROM shadow_replay_outcomes")
    db.conn.commit()
    written = 0
    per_engine: dict[str, Counter] = {e["style"]: Counter() for e in _ENGINES}

    warnings.filterwarnings(
        "ignore", message="X does not have valid feature names", category=UserWarning
    )
    for i, row in enumerate(rows):
        if not bool(row.get("fired", True)):
            continue
        epic = str(row.get("epic") or "")
        market = str(row.get("market") or "")
        side = str(row.get("direction") or "")
        setup_key = str(row.get("setup_key") or "")
        session = str(row.get("session_window") or "")
        ts = str(row.get("timestamp") or str(i))

        # Genuine entry-decision score from the live model on widened features.
        feats = features_from_replay_row(row)
        # Pad any model feature not present (neutral 0) so predict never skips.
        if scorer.is_trained():
            for fname in scorer.feature_names:
                feats.setdefault(fname, 0.0)
            ml_score = scorer.predict(feats)
        else:
            ml_score = None

        for eng in _ENGINES:
            result = str(row.get(eng["label"]) or "").upper()
            if result not in ("WIN", "LOSS", "BREAKEVEN"):
                continue
            hold_sec = round(bar_sec * eng["horizon"], 1)
            ref = f"RPLY-{eng['style']}-{epic}-{i}-{ts}"
            ok = upsert_shadow_replay(
                db.conn,
                commit=False,
                row={
                    "replay_ref": ref,
                    "source_ref": ts,
                    "style": eng["style"],
                    "engine": eng["engine"],
                    "epic": epic,
                    "market": market,
                    "side": side,
                    "result": result,
                    "ml_score_at_entry": ml_score,
                    "hold_sec": hold_sec,
                    "horizon_bars": eng["horizon"],
                    "setup_key": setup_key,
                    "session_window": session,
                },
            )
            if ok:
                written += 1
                per_engine[eng["style"]][result] += 1

    db.conn.commit()
    summary = {
        "ok": True,
        "source": str(src),
        "source_rows": len(rows),
        "bar_interval_sec": bar_sec,
        "written": written,
        "per_engine": {k: dict(v) for k, v in per_engine.items()},
        "shadow_replay_counts": replay_style_epic_counts(db.conn),
    }
    return summary


# ---------------------------------------------------------------------------
# Training run (gated) + offline shadow metrics
# ---------------------------------------------------------------------------
def _brier_score(y_true: list[int], y_score: list[float]) -> float | None:
    if not y_true:
        return None
    total = 0.0
    for t, s in zip(y_true, y_score):
        p = min(max(float(s), 0.0), 1.0)
        total += (p - float(t)) ** 2
    return total / len(y_true)


def _shadow_metrics_by_style() -> dict[str, Any]:
    """Offline AUC/Brier of the live model score vs shadow replay labels per style."""
    from data.learning_store import LearningStore
    from data.shadow_training_registry import ensure_replay_schema
    from trading.ml_scorer import _holdout_metrics

    db = LearningStore(str(_learning_db_path()))
    cur = db.conn.cursor()
    ensure_replay_schema(cur)
    out: dict[str, Any] = {}
    for style in (SNIPER, LONG):
        rows = cur.execute(
            "SELECT result, ml_score_at_entry, epic FROM shadow_replay_outcomes "
            "WHERE is_shadow=1 AND style=? AND UPPER(result) IN ('WIN','LOSS') "
            "AND ml_score_at_entry IS NOT NULL",
            (style,),
        ).fetchall()
        y = [1 if str(r["result"]).upper() == "WIN" else 0 for r in rows]
        s = [float(r["ml_score_at_entry"]) for r in rows]
        wins = sum(y)
        by_epic: Counter[str] = Counter()
        for r in rows:
            by_epic[str(r["epic"] or "?")] += 1
        block: dict[str, Any] = {
            "labeled": len(y),
            "wins": wins,
            "losses": len(y) - wins,
            "win_rate": round(wins / len(y), 4) if y else None,
            "auc": None,
            "logloss": None,
            "brier": None,
            "by_epic": dict(by_epic),
        }
        if len(set(y)) > 1 and len(y) >= 10:
            auc, ll = _holdout_metrics(y, s)
            block["auc"] = round(auc, 4) if auc is not None else None
            block["logloss"] = round(ll, 4) if ll is not None else None
            brier = _brier_score(y, s)
            block["brier"] = round(brier, 4) if brier is not None else None
        out[style] = block
    return out


def _export_widened_replay_csv(out_path: Path) -> dict[str, Any]:
    """Export fired WIN/LOSS replay rows with the widened feature vector."""
    src = next((p for p in _REPLAY_SOURCES if p.is_file()), None)
    if src is None:
        return {"rows": 0, "reason": "no_replay_results"}
    rows_out: list[dict[str, Any]] = []
    epic_counts: Counter[str] = Counter()
    style_proxy = Counter()  # label_3bar vs label_6bar availability
    for row in _read_jsonl(src):
        if not bool(row.get("fired", True)):
            continue
        feats = features_from_replay_row(row)
        # Prefer sniper (3-bar) label for the live model refresh; long labels are
        # evaluated separately in the dual-engine shadow plane.
        label = str(row.get("label_3bar") or row.get("label_3") or "").upper()
        if label not in ("WIN", "LOSS"):
            continue
        rec = {
            "label": label,
            "fired": 1,
            "timestamp": row.get("timestamp"),
            "epic": row.get("epic"),
            "atr": row.get("atr"),
            "spread": row.get("spread"),
            "stop_pts": row.get("stop_pts"),
            **feats,
        }
        rows_out.append(rec)
        epic_counts[str(row.get("epic") or "?")] += 1
        style_proxy["sniper_label_3bar"] += 1

    if not rows_out:
        return {"rows": 0, "reason": "no_win_loss_fired"}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({k for r in rows_out for k in r})
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows_out:
            writer.writerow(r)
    return {
        "rows": len(rows_out),
        "path": str(out_path),
        "by_epic": dict(epic_counts),
        "label_source": dict(style_proxy),
        "feature_names": list(FEATURE_NAMES),
    }


def _xgboost_status() -> dict[str, Any]:
    try:
        import xgboost

        return {"ok": True, "version": str(xgboost.__version__)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def train() -> dict[str, Any]:
    from diagnostics.ml_strategy_review import (
        improvement_epoch_eligible_for_verdict,
        load_latest_review_verdict,
    )
    from ml.auto_trainer import count_win_loss_labels, train_model_from_store
    from system.config_loader import get_config
    from trading.ml_scorer import get_ml_scorer, reload_ml_scorer

    verdict, vpath = load_latest_review_verdict(Path(data_dir()))
    eligible = improvement_epoch_eligible_for_verdict(verdict)

    cfg = None
    try:
        cfg = get_config()
    except Exception:
        cfg = None

    live_labels = count_win_loss_labels()
    xgb = _xgboost_status()

    # Primary refresh: widened multi-epic replay WIN/LOSS (honest offline edge hunt).
    csv_path = data_dir() / "ml_widened_replay_train.csv"
    export_info = _export_widened_replay_csv(csv_path)
    widened_train: dict[str, Any] = {"ok": False, "export": export_info}
    if int(export_info.get("rows") or 0) >= 30:
        try:
            scorer = get_ml_scorer()
            scorer.train(csv_path)
            scorer = reload_ml_scorer()
            widened_train = {
                "ok": True,
                "labels": export_info["rows"],
                "features": list(scorer.feature_names),
                "backend_meta": _read_model_backend(),
                "export": export_info,
                "trained": scorer.is_trained(),
            }
            try:
                from runtime.strategy_improvement_tracker import note_ml_model_trained

                # Epoch gate: refresh allowed; never claim improvement under
                # NOT_MEASURABLE / ineligible verdicts.
                # Harness never claims an improvement epoch — refresh only.
                note_ml_model_trained(
                    improvement_epoch=False,
                    review_verdict=verdict,
                )
            except Exception:
                pass
        except Exception as exc:
            widened_train = {
                "ok": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "export": export_info,
            }

    # Secondary: live-store trainer (may be thin); does not override widened refresh
    # when widened succeeded — reported for inventory honesty.
    live_train_result = train_model_from_store(cfg) if not widened_train.get("ok") else {
        "ok": False,
        "reason": "skipped_widened_refresh_primary",
        "labels": live_labels,
    }

    return {
        "review_verdict": verdict,
        "review_path": str(vpath) if vpath else None,
        "improvement_epoch_eligible": eligible,
        "improvement_epoch_claimed": False,  # never claimed here
        "xgboost": xgb,
        "live_win_loss_labels": live_labels,
        "widened_train_result": widened_train,
        "live_train_result": live_train_result,
        "feature_names": list(FEATURE_NAMES),
        # Shadow metrics require a post-train `replay` pass so scores match the
        # refreshed model — see `all` / caller.
        "shadow_metrics_by_style": None,
        "note": "run replay after train to populate shadow_metrics_by_style",
    }


def _read_model_backend() -> dict[str, Any]:
    meta = data_dir() / "ml_model" / "meta.json"
    if not meta.is_file():
        # Symlink / legacy bridge
        alt = project_root() / "src" / "data" / "ml_model" / "meta.json"
        meta = alt if alt.is_file() else meta
    if not meta.is_file():
        return {}
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("inventory", help="read-only counts per style/epic")
    bf = sub.add_parser("backfill", help="stamp style/epic on existing rows")
    bf.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    sub.add_parser(
        "build",
        help="forward-label Gold/DOW/Nikkei from OHLC + stamp widened features",
    )
    rp = sub.add_parser("replay", help="dual-engine replay over real market data")
    rp.add_argument("--limit", type=int, default=None)
    sub.add_parser("train", help="gated widened/xgboost refresh (run replay after)")
    sub.add_parser("metrics", help="offline shadow AUC/Brier by style/epic")
    allp = sub.add_parser(
        "all",
        help="backfill --apply + build + train + replay + metrics",
    )
    allp.add_argument("--limit", type=int, default=None)
    # Accept --json on parent or any subcommand (operators often put flags last).
    for _p in (ap, bf, rp, allp):
        _p.add_argument(
            "--json",
            action="store_true",
            default=False,
            help="emit JSON only",
        )
    ap.add_argument("--all", action="store_true", help="alias for the 'all' subcommand")
    args = ap.parse_args(argv)
    # Unify --json whether passed before or after the subcommand.
    if not getattr(args, "json", False):
        args.json = False

    cmd = args.cmd
    if getattr(args, "all", False) and not cmd:
        cmd = "all"
    if not cmd:
        cmd = "inventory"

    result: dict[str, Any] = {"cmd": cmd, "generated_at": datetime.now(timezone.utc).isoformat()}

    if cmd == "inventory":
        rep = inventory()
        result["inventory"] = rep
        if not args.json:
            _print_inventory(rep)
    elif cmd == "backfill":
        rep = backfill_ml_training_store(apply=bool(args.apply))
        result["backfill"] = rep
        if not args.json:
            print("=== BACKFILL ===")
            print(json.dumps(rep, indent=2, default=str))
    elif cmd == "build":
        rep = build_multi_epic_replay()
        result["build"] = rep
        if not args.json:
            print("=== BUILD (multi-epic forward labels) ===")
            print(json.dumps(rep, indent=2, default=str))
    elif cmd == "replay":
        rep = replay_both_engines(limit=args.limit)
        result["replay"] = rep
        result["shadow_metrics_by_style"] = _shadow_metrics_by_style()
        if not args.json:
            print("=== REPLAY (dual-engine) ===")
            print(json.dumps(rep, indent=2, default=str))
            print("=== SHADOW METRICS ===")
            print(json.dumps(result["shadow_metrics_by_style"], indent=2, default=str))
    elif cmd == "train":
        rep = train()
        result["train"] = rep
        if not args.json:
            print("=== TRAIN (gated) ===")
            print(json.dumps(rep, indent=2, default=str))
    elif cmd == "metrics":
        rep = _shadow_metrics_by_style()
        result["shadow_metrics_by_style"] = rep
        if not args.json:
            print("=== SHADOW METRICS ===")
            print(json.dumps(rep, indent=2, default=str))
    elif cmd == "all":
        # Order: build labels/features → train widened model → score both engines.
        result["backfill"] = backfill_ml_training_store(apply=True)
        result["build"] = build_multi_epic_replay()
        result["train"] = train()
        result["replay"] = replay_both_engines(limit=getattr(args, "limit", None))
        result["shadow_metrics_by_style"] = _shadow_metrics_by_style()
        result["inventory"] = inventory()
        result["xgboost"] = _xgboost_status()
        if not args.json:
            print(json.dumps(result, indent=2, default=str))

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
