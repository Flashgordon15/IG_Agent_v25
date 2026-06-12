"""Learning pipeline health — ML, registry, agent P&L, sentiment, calendar."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.learning_trade_policy import agent_trades_sql_clause
from system.protective_learning import snapshot as protective_snapshot
from system.setup_registry import load_registry


def _learning_store():
    from data.learning_store import LearningStore
    from system.paths import data_dir

    return LearningStore(data_dir() / "learning_db.sqlite3")


def _agent_pnl_summary(store: Any) -> dict[str, Any]:
    clause = agent_trades_sql_clause()
    row = store.conn.execute(
        f"""
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses
        FROM trades
        WHERE closed_at IS NOT NULL AND {clause}
        """
    ).fetchone()
    ig_row = store.conn.execute(
        """
        SELECT COUNT(*) AS n FROM trades
        WHERE closed_at IS NOT NULL
          AND (
            UPPER(setup_key) LIKE 'IG|%'
            OR UPPER(setup_key) IN ('IG_IMPORT', 'IG|IMPORTED')
            OR LOWER(COALESCE(source,'')) IN ('ig_import', 'ig|imported')
          )
        """
    ).fetchone()
    n = int(row["n"] or 0) if row else 0
    wins = int(row["wins"] or 0) if row else 0
    losses = int(row["losses"] or 0) if row else 0
    ig_n = int(ig_row["n"] or 0) if ig_row else 0
    wr = round(wins / n, 4) if n else 0.0
    return {
        "agent_closed_trades": n,
        "agent_wins": wins,
        "agent_losses": losses,
        "agent_win_rate": wr,
        "ig_import_rows_excluded": ig_n,
    }


def _ml_status() -> dict[str, Any]:
    from data.ml_training_store import MLTrainingStore
    from system.ml_filter_overrides import load_filter_overrides
    from system.paths import data_dir
    from trading.ml_scorer import get_ml_scorer

    scorer = get_ml_scorer()
    records = MLTrainingStore().record_count()
    meta_path = data_dir() / "ml_model" / "meta.json"
    model_path = data_dir() / "ml_model" / "model.pkl"
    training_meta = data_dir() / "ml_model" / "training_meta.json"
    min_records = 500
    s4_manifest = (
        Path(__file__).resolve().parents[2]
        / "data_lake"
        / "models"
        / "s4"
        / "manifest.json"
    )
    return {
        "use_ml_signal": bool(
            __import__("system.config_loader", fromlist=["get_config"])
            .get_config()
            .get("USE_ML_SIGNAL")
        ),
        "model_trained": scorer.is_trained(),
        "model_path_exists": model_path.is_file(),
        "training_records": records,
        "training_records_required": min_records,
        "ml_blend_ready": scorer.is_trained() and records >= min_records,
        "filter_overrides_active": bool(load_filter_overrides()),
        "meta_path": str(meta_path),
        "s4_manifest_exists": s4_manifest.is_file(),
        "training_meta_exists": training_meta.is_file(),
    }


def _registry_status() -> dict[str, Any]:
    reg = load_registry(force=True)
    setups = reg.get("setups") or {}
    banned = reg.get("banned_keys") or []
    return {
        "enabled": bool(reg.get("enabled")),
        "generated_at": reg.get("generated_at"),
        "rolling_days": reg.get("rolling_days"),
        "setups_tracked": len(setups) if isinstance(setups, dict) else 0,
        "banned_count": len(banned) if isinstance(banned, list) else 0,
        "banned_keys": list(banned)[:20] if isinstance(banned, list) else [],
    }


def _policy_status() -> dict[str, Any]:
    from system.gate_relaxation import relaxation_snapshot
    from system.learning_demo_policy import effective_policy_snapshot

    store = None
    try:
        store = _learning_store()
    except Exception:
        pass
    return {
        "learning_demo": effective_policy_snapshot(store),
        "relaxations": relaxation_snapshot(),
        "protective_learning": protective_snapshot(),
    }


def _sentiment_status() -> dict[str, Any]:
    from system.config_loader import get_config

    block = get_config().get("sentiment_guard") or {}
    return {
        "configured": bool(block.get("enabled", True)),
        "crowded_long_pct": block.get("crowded_long_pct", 80),
        "crowded_short_pct": block.get("crowded_short_pct", 20),
        "note": "Live values appear on dashboard environment_fitness gate",
    }


def _calendar_status() -> dict[str, Any]:
    from system.calendar_gate import is_calendar_blocked
    from system.v26_config import calendar_settings

    cfg = calendar_settings()
    blocked, reason = is_calendar_blocked("")
    return {
        "enabled": bool(cfg.get("enabled")),
        "currently_blocked": blocked,
        "detail": reason if blocked else "no active window",
    }


def _recommendations(report: dict[str, Any]) -> list[str]:
    out: list[str] = []
    ml = report.get("ml") or {}
    reg = report.get("setup_registry") or {}
    pnl = report.get("agent_pnl") or {}
    if not ml.get("ml_blend_ready"):
        out.append(
            "ML blend inactive — train model (≥500 labels) via replay/S4 retrain pipeline."
        )
    if not reg.get("enabled"):
        out.append(
            "Setup registry gate is off — run refresh_setup_registry to ban bad setups."
        )
    elif (reg.get("banned_count") or 0) == 0:
        out.append(
            "No banned setups yet — need more agent-labelled closes per setup_key."
        )
    if (pnl.get("agent_closed_trades") or 0) < 30:
        out.append(
            "Few agent-sourced closes — learning penalties need ≥10 trades/setup; "
            "IG imports are excluded from stats."
        )
    if (pnl.get("ig_import_rows_excluded") or 0) > 0:
        out.append(
            f"{pnl['ig_import_rows_excluded']} IG-import rows excluded from learning — "
            "review Trades tab with agent-only filter."
        )
    prot = (report.get("policy") or {}).get("protective_learning") or {}
    if not prot.get("enabled"):
        out.append(
            "Protective learning profile is off — gates remain in demo soak mode."
        )
    return out


def build_learning_health_report(*, refresh_registry: bool = False) -> dict[str, Any]:
    store = _learning_store()
    registry_refresh = None
    if refresh_registry:
        from system.setup_registry_refresh import refresh_setup_registry_from_store

        registry_refresh = refresh_setup_registry_from_store(store, enabled=True)

    report: dict[str, Any] = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_pnl": _agent_pnl_summary(store),
        "ml": _ml_status(),
        "setup_registry": _registry_status(),
        "policy": _policy_status(),
        "sentiment": _sentiment_status(),
        "calendar": _calendar_status(),
    }
    if registry_refresh:
        report["registry_refresh"] = registry_refresh
    report["recommendations"] = _recommendations(report)
    return report
