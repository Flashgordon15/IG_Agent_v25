"""v30.0 CERT tab — ML certification ladder from isolated v30 data lake."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.paths import data_lake_dir, project_root


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _level(id_: str, name: str, pct: int, status: str, detail: str) -> dict[str, Any]:
    return {"id": id_, "name": name, "pct": pct, "status": status, "detail": detail}


def build_v30_cert_payload() -> dict[str, Any]:
    lake = data_lake_dir()
    roadmap = _read_json(lake / "state" / "roadmap_progress_latest.json")
    learning = _read_json(lake / "state" / "v26_learning_snapshot.json")
    s4_manifest = _read_json(lake / "models" / "s4" / "manifest.json")
    ml_records = 0
    try:
        from data.ml_training_store import MLTrainingStore

        ml_records = MLTrainingStore().record_count()
    except Exception:
        pass

    milestone = str(roadmap.get("milestone") or "M0")
    overall = int(roadmap.get("overall_pct") or 0)
    feeder = roadmap.get("feeder_today") or {}
    prof = roadmap.get("profitability_14d") or {}

    l0_pct = min(100, max(0, overall))
    l1_pct = min(100, int((ml_records / 500) * 100)) if ml_records else 0
    l2_pct = min(100, int(feeder.get("signal_evals", 0) / 200)) if feeder else 0
    l3_pct = min(100, int((prof.get("trades") or 0) * 5))
    l4_pct = 100 if s4_manifest.get("version") or s4_manifest.get("trained_at") else 0
    l5_pct = min(100, l4_pct) if prof.get("wr_pct", 0) >= 55 else max(0, l4_pct - 20)

    levels = [
        _level("L0", "Platform boot & gates", l0_pct, "PASS" if l0_pct >= 40 else "IN_PROGRESS", f"Milestone {milestone} · {overall}% roadmap"),
        _level("L1", "ML training corpus", l1_pct, "PASS" if ml_records >= 500 else "IN_PROGRESS", f"{ml_records}/500 confirmed feature vectors"),
        _level("L2", "Feeder signal density", l2_pct, "PASS" if l2_pct >= 50 else "IN_PROGRESS", f"{feeder.get('signal_evals', 0)} evals today"),
        _level("L3", "Live profitability", l3_pct, "PASS" if (prof.get('trades') or 0) >= 20 else "NOT_STARTED", f"{prof.get('trades', 0)} closed trades (14d)"),
        _level("L4", "S4 model deployment", l4_pct, "PASS" if l4_pct >= 100 else "INSUFFICIENT", str(s4_manifest.get("version") or "awaiting model manifest")),
        _level("L5", "Production certification", l5_pct, "NOT_STARTED" if l5_pct < 80 else "IN_PROGRESS", "Target L5 — forward walk validation"),
    ]

    focus = learning.get("learning_focus") or []
    if not focus:
        focus = [
            "v30 data lake namespace active under v30-production/data_lake/",
            f"Feeder would_fire today: {feeder.get('trade_ready', 0)}",
            f"Gate blockers tracked: {len((roadmap.get('gate_blockers_7d') or {}).get('top') or [])}",
        ]

    return {
        "ok": True,
        "version": "30.0",
        "target": "L5",
        "current_milestone": milestone,
        "levels": levels,
        "learning_focus": focus[:8],
        "data_lake_root": str(lake),
        "ml_records": ml_records,
        "roadmap_pct": overall,
    }
