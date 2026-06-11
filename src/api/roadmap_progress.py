"""Roadmap progress — daily gap audit for £1k/day certification (dashboard + snapshots)."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from system.paths import data_dir, project_root


def _ensure_v26_path() -> None:
    root = project_root()
    v26 = str(root / "v26")
    if v26 not in sys.path:
        sys.path.insert(0, v26)


def _history_path() -> Path:
    p = data_dir() / "state" / "roadmap_progress_history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_history(days: int = 7) -> list[dict[str, Any]]:
    path = _history_path()
    if not path.is_file():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date().isoformat()
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            day = str(row.get("day") or row.get("generated_at", "")[:10])
            if day >= cutoff:
                rows.append(row)
    except (OSError, json.JSONDecodeError):
        pass
    return rows[-max(1, days) :]


def append_daily_snapshot(payload: dict[str, Any]) -> None:
    """Append one row per UTC day (replace same-day row)."""
    path = _history_path()
    day = str(payload.get("day") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    slim = {
        "day": day,
        "generated_at": payload.get("generated_at"),
        "overall_pct": payload.get("overall_pct"),
        "milestone": payload.get("milestone"),
        "sections": [
            {
                "id": s.get("id"),
                "pct": s.get("pct"),
                "items": [
                    {"id": i.get("id"), "pct": i.get("pct"), "status": i.get("status")}
                    for i in (s.get("items") or [])
                ],
            }
            for s in (payload.get("sections") or [])
        ],
    }
    existing: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("day") or "") != day:
                existing.append(line)
    existing.append(json.dumps(slim, separators=(",", ":")))
    path.write_text("\n".join(existing) + "\n", encoding="utf-8")


def _pct(num: float, den: float, *, cap: float = 100.0) -> int:
    if den <= 0:
        return 0
    return int(min(cap, max(0, round(100.0 * float(num) / float(den)))))


def _item(
    *,
    id_: str,
    label: str,
    pct: int,
    status: str,
    detail: str,
    action: str,
    metric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "label": label,
        "pct": int(max(0, min(100, pct))),
        "status": status,
        "detail": detail,
        "action": action,
        "metric": metric or {},
    }


def _profitability_14d() -> dict[str, Any]:
    db = data_dir() / "learning_db.sqlite3"
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    out: dict[str, Any] = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "wr_pct": 0.0,
        "net_gbp": 0.0,
        "epics_traded": 0,
        "epics_enabled": 0,
        "by_epic": {},
    }
    if not db.is_file():
        return out
    try:
        from system.config_loader import get_config

        cfg = get_config()
        enabled = [
            str(v.get("epic") or "")
            for v in (cfg.as_dict().get("instruments") or {}).values()
            if v.get("enabled") and v.get("epic")
        ]
        out["epics_enabled"] = len(enabled)
    except Exception:
        enabled = []

    try:
        with sqlite3.connect(str(db), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT epic, market, result,
                       COALESCE(ig_pnl_currency, 0) AS gbp
                FROM trades
                WHERE dry_run = 0
                  AND result IS NOT NULL AND result != 'OPEN'
                  AND closed_at >= ?
                """,
                (since,),
            ).fetchall()
    except sqlite3.Error:
        return out

    epic_stats: dict[str, dict[str, Any]] = {}
    wins = losses = 0
    net = 0.0
    for r in rows:
        epic = str(r["epic"] or r["market"] or "?")
        res = str(r["result"] or "").upper()
        gbp = float(r["gbp"] or 0)
        net += gbp
        st = epic_stats.setdefault(epic, {"w": 0, "l": 0, "gbp": 0.0})
        st["gbp"] += gbp
        if res == "WIN":
            wins += 1
            st["w"] += 1
        elif res == "LOSS":
            losses += 1
            st["l"] += 1
    out["trades"] = wins + losses
    out["wins"] = wins
    out["losses"] = losses
    out["wr_pct"] = round(100.0 * wins / (wins + losses), 1) if wins + losses else 0.0
    out["net_gbp"] = round(net, 2)
    out["epics_traded"] = len(epic_stats)
    out["by_epic"] = epic_stats
    return out


def _feeder_today() -> dict[str, Any]:
    try:
        _ensure_v26_path()
        from ingest.lake_reader import summarize_day, utc_today

        s = summarize_day(utc_today())
        return {
            "day": s.day,
            "signal_evals": s.signal_evals,
            "trade_ready": getattr(s, "trade_ready", s.would_fire),
            "signal_actionable": getattr(s, "signal_actionable", 0),
            "order_intents": s.order_intents,
            "fill_closes": s.fill_closes,
            "fill_pnl_gbp": round(float(s.fill_pnl_gbp), 2),
        }
    except Exception:
        return {}


def _gate_blockers(days: int = 7) -> dict[str, Any]:
    try:
        scripts = str(project_root() / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from gate_attribution_report import rollup_gate_blocks

        log = project_root() / "src" / "data" / "logs" / "engine.log"
        return rollup_gate_blocks(log_path=log, days=max(1, days))
    except Exception:
        return {"ranked_blockers": [], "trace_blockers": []}


def _ml_training_rows() -> int:
    try:
        from data.ml_training_store import MLTrainingStore

        return int(MLTrainingStore().record_count())
    except Exception:
        return 0


def build_roadmap_progress(*, history_days: int = 7, write_snapshot: bool = False) -> dict[str, Any]:
    """Aggregate certification, P&L, gates, and feeder into dashboard checklist."""
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    cert_levels: list[dict[str, Any]] = []
    milestone = "M0"
    try:
        _ensure_v26_path()
        from research.certification import build_certification_payload

        cert = build_certification_payload()
        cert_levels = list(cert.get("levels") or [])
        milestone = str(cert.get("current_milestone") or "M0")
    except Exception:
        pass

    prof = _profitability_14d()
    feeder = _feeder_today()
    gates = _gate_blockers(7)
    ml_rows = _ml_training_rows()
    history = _read_history(history_days)

    try:
        from system.gate_relaxation import demo_soak_enabled, relaxation_snapshot

        relax = relaxation_snapshot()
        soak_on = demo_soak_enabled()
    except Exception:
        relax = {}
        soak_on = False

    ranked = list(gates.get("ranked_blockers") or [])
    top_block = ranked[0] if ranked else {}
    top_gate = str(top_block.get("gate") or "none")
    top_pct = float(top_block.get("pct") or 0)

    # --- sections ---
    cert_items: list[dict[str, Any]] = []
    cert_pcts: list[int] = []
    for lv in cert_levels:
        pct = int(lv.get("pct") or 0)
        cert_pcts.append(pct)
        status = str(lv.get("status") or "PENDING")
        cert_items.append(
            _item(
                id_=str(lv.get("id") or ""),
                label=str(lv.get("name") or lv.get("id") or ""),
                pct=pct,
                status=status,
                detail=str(lv.get("detail") or ""),
                action="Keep agent running; nightly cert refresh" if status != "PASS" else "Maintain",
            )
        )
    cert_section_pct = int(sum(cert_pcts) / len(cert_pcts)) if cert_pcts else 0

    wr = float(prof.get("wr_pct") or 0)
    net = float(prof.get("net_gbp") or 0)
    trades = int(prof.get("trades") or 0)
    edge_items = [
        _item(
            id_="wr_14d",
            label="Win rate (14d)",
            pct=_pct(wr, 52.0),
            status="PASS" if wr >= 52 else "IN_PROGRESS",
            detail=f"{wr:.1f}% over {trades} trades (target ≥52%)",
            action="Ban negative-E£ setups; review Japan SELL asia_early",
        ),
        _item(
            id_="net_14d",
            label="Net P&L (14d)",
            pct=_pct(max(0, net), 1000.0),
            status="PASS" if net >= 200 else "IN_PROGRESS",
            detail=f"£{net:+.2f} net (M1 target £200+ rolling)",
            action="Improve edge before scaling size",
        ),
        _item(
            id_="ml_rows",
            label="ML training rows",
            pct=_pct(ml_rows, 500),
            status="PASS" if ml_rows >= 500 else "IN_PROGRESS",
            detail=f"{ml_rows}/500 rows for blend + S4 veto",
            action="Keep demo soak trading; nightly learning pack",
        ),
    ]
    edge_pct = int(sum(i["pct"] for i in edge_items) / len(edge_items))

    epics_on = max(1, int(prof.get("epics_enabled") or 1))
    epics_traded = int(prof.get("epics_traded") or 0)
    coverage_items = [
        _item(
            id_="epic_coverage",
            label="Markets with closes (14d)",
            pct=_pct(epics_traded, min(7, epics_on)),
            status="PASS" if epics_traded >= 3 else "IN_PROGRESS",
            detail=f"{epics_traded}/{epics_on} enabled epics traded",
            action="Verify Gold, FX, US indices fire in-session (soak on)",
        ),
        _item(
            id_="soak_mode",
            label="Demo soak active",
            pct=100 if soak_on else 0,
            status="PASS" if soak_on else "WARN",
            detail="Rotation bypass + ML veto bypass" if soak_on else "Enable for probe flow",
            action="Set demo_soak_mode.enabled true; restart agent",
        ),
    ]
    if soak_on and top_gate == "active_rotation" and top_pct > 5:
        coverage_items.append(
            _item(
                id_="rotation_blocks",
                label="Rotation blocks (7d)",
                pct=max(0, 100 - int(top_pct)),
                status="WARN",
                detail=f"{top_pct:.0f}% WAITs still rotation — restart agent?",
                action="Restart agent after config change",
            )
        )
    coverage_pct = int(sum(i["pct"] for i in coverage_items) / len(coverage_items))

    tr = int(feeder.get("trade_ready") or 0)
    intents = int(feeder.get("order_intents") or 0)
    fills = int(feeder.get("fill_closes") or 0)
    flow_items = [
        _item(
            id_="trade_ready_today",
            label="Trade-ready signals (today)",
            pct=min(100, tr),
            status="PASS" if tr > 0 else "IN_PROGRESS",
            detail=f"{tr} all-gates-pass evals today",
            action="Trade during session windows; check gate blockers",
        ),
        _item(
            id_="order_intents_today",
            label="Order intents (today)",
            pct=100 if intents > 0 else (50 if tr > 0 else 0),
            status="PASS" if intents > 0 else "IN_PROGRESS",
            detail=f"{intents} intents · {fills} closes · £{feeder.get('fill_pnl_gbp', 0):+.2f}",
            action="If trade_ready>0 but intents=0: check execution log",
        ),
    ]
    if ranked:
        flow_items.append(
            _item(
                id_="top_blocker",
                label=f"Top blocker: {top_gate}",
                pct=max(0, 100 - int(top_pct)),
                status="WARN" if top_pct > 40 else "IN_PROGRESS",
                detail=str(top_block.get("sample") or "")[:100],
                action="Session blocks expected OOH; fix rotation via soak restart",
            )
        )
    flow_pct = int(sum(i["pct"] for i in flow_items) / len(flow_items))

    sections = [
        {"id": "certification", "title": "Certification ladder", "pct": cert_section_pct, "items": cert_items},
        {"id": "edge", "title": "Edge & ML data", "pct": edge_pct, "items": edge_items},
        {"id": "coverage", "title": "Market coverage", "pct": coverage_pct, "items": coverage_items},
        {"id": "flow", "title": "Today's trading flow", "pct": flow_pct, "items": flow_items},
    ]
    overall = int(sum(s["pct"] for s in sections) / len(sections))

    payload: dict[str, Any] = {
        "ok": True,
        "generated_at": ts,
        "day": day,
        "overall_pct": overall,
        "milestone": milestone,
        "target_daily_gbp": 1000,
        "stretch_daily_gbp": 250,
        "relaxation": relax,
        "profitability_14d": prof,
        "feeder_today": feeder,
        "gate_blockers_7d": {
            "top": ranked[:5],
            "trace": (gates.get("trace_blockers") or [])[:5],
        },
        "sections": sections,
        "history": history,
    }

    if write_snapshot:
        append_daily_snapshot(payload)

    return payload
