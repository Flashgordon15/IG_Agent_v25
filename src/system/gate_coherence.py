"""
Trading gate coherence audit — config/rules alignment before market conditions apply.

Prevents silent 'no trades' from code/config drift (portfolio bug class, points 92% surprise, etc.).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from system.paths import project_root

Severity = Literal["CRITICAL", "WARNING", "INFO"]


@dataclass
class CoherenceIssue:
    severity: Severity
    code: str
    message: str
    remedy: str = ""


@dataclass
class MarketCoherence:
    instrument_id: str
    epic: str
    market: str
    ok: bool
    current_session: str
    session_allowed: bool
    effective_confidence_threshold: float
    config_signal_threshold: float
    points_state: str
    fitness_floor_pct: float
    ml_veto_active: bool
    ml_veto_model_ready: bool
    stop_distance_pts: float
    risk_cap_gbp: float
    issues: list[CoherenceIssue] = field(default_factory=list)


@dataclass
class CoherenceReport:
    ok: bool
    issues: list[CoherenceIssue] = field(default_factory=list)
    markets: list[MarketCoherence] = field(default_factory=list)
    generated_at: str = ""

    @property
    def critical(self) -> list[CoherenceIssue]:
        return [i for i in self.issues if i.severity == "CRITICAL"]

    @property
    def warnings(self) -> list[CoherenceIssue]:
        return [i for i in self.issues if i.severity == "WARNING"]


def _load_v26() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def repair_corrupt_trade_rows(store: Any, cfg: Any) -> int:
    """Fix empty epic / stop=0 rows that poison portfolio rehydrate."""
    from execution.trade_risk import infer_epic_from_row, resolve_stop_price

    try:
        rows = store.conn.execute(
            """
            SELECT id, epic, entry, stop, side, dry_run
            FROM trades
            WHERE dry_run = 0
              AND (
                epic IS NULL OR epic = ''
                OR stop IS NULL OR stop = 0
                OR (stop = entry AND entry > 0)
              )
            """
        ).fetchall()
    except Exception:
        return 0

    fixed = 0
    for row in rows:
        rid = int(row["id"])
        epic = str(row["epic"] or "").strip() or infer_epic_from_row(row, cfg)
        if not epic:
            continue
        try:
            entry = float(row["entry"] or 0)
            side = str(row["side"] or "BUY")
            stop_raw = float(row["stop"] or 0)
        except (TypeError, ValueError):
            continue
        new_stop = resolve_stop_price(
            entry=entry,
            side=side,
            stop_level=stop_raw,
            epic=epic,
            cfg=cfg,
        )
        if new_stop <= 0:
            continue
        store.conn.execute(
            "UPDATE trades SET epic=?, stop=? WHERE id=?",
            (epic, new_stop, rid),
        )
        fixed += 1
    if fixed:
        store.conn.commit()
    return fixed


def _effective_confidence_threshold(
    cfg: Any,
    inst: dict[str, Any],
    *,
    points_state: str,
) -> float:
    from trading.points_engine import CONF_HIGH, CONF_MARGINAL_MIN

    sig = float(inst.get("signal_threshold") or getattr(cfg, "signal_threshold", 80))
    pts = (points_state or "HEALTHY").upper()
    if pts == "STOP":
        return 100.0
    if pts == "WARNING":
        base_th = CONF_HIGH
    else:
        base_th = max(sig, CONF_MARGINAL_MIN)
    try:
        from system.gate_relaxation import effective_trade_confidence_threshold

        return effective_trade_confidence_threshold(
            base_th,
            points_state=pts,
            instrument_threshold=sig,
        )
    except Exception:
        return base_th


def audit_market(
    *,
    instrument_id: str,
    inst: dict[str, Any],
    cfg: Any,
    points_state: str,
    v26: dict[str, Any],
    current_session: str,
    s4_veto_epics: set[str],
) -> MarketCoherence:
    """Per-epic rule alignment — sessions, thresholds, fitness, ml_veto."""
    epic = str(inst.get("epic") or "").strip()
    market = str(inst.get("name") or instrument_id)
    issues: list[CoherenceIssue] = []
    wl = list(inst.get("trading_session_whitelist") or [])
    session_ok = bool(wl and current_session in wl)

    if not epic:
        issues.append(
            CoherenceIssue(
                "CRITICAL",
                "missing_epic",
                f"{instrument_id}: no epic configured",
            )
        )
    if not wl:
        issues.append(
            CoherenceIssue(
                "WARNING",
                "no_session_whitelist",
                f"{market}: no trading_session_whitelist",
            )
        )
    elif not session_ok:
        issues.append(
            CoherenceIssue(
                "INFO",
                "session_closed",
                f"{market}: session '{current_session}' not in {wl}",
                remedy="Expected off-window unless market hours changed",
            )
        )

    sig_th = float(inst.get("signal_threshold") or 80)
    eff_th = _effective_confidence_threshold(cfg, inst, points_state=points_state)
    if eff_th >= 92 and points_state == "WARNING":
        issues.append(
            CoherenceIssue(
                "WARNING",
                "threshold_92",
                f"{market}: effective threshold {eff_th:.0f}% (points WARNING)",
            )
        )
    if sig_th > eff_th + 5:
        issues.append(
            CoherenceIssue(
                "INFO",
                "threshold_stack",
                f"{market}: config {sig_th:.0f}% vs effective {eff_th:.0f}%",
            )
        )

    try:
        from system.gate_relaxation import effective_fitness_min

        fitness = effective_fitness_min(epic, points_state=points_state)
    except Exception:
        fitness = 55.0

    ml_cfg = v26.get("ml_veto") or {}
    per = ml_cfg.get("per_epic") or {}
    ml_on = bool(
        ml_cfg.get("enabled")
        and epic
        and (
            epic in per and isinstance(per.get(epic), dict) and per[epic].get("enabled")
        )
    )
    ml_ready = epic in s4_veto_epics if epic else False
    if ml_on and not ml_ready:
        issues.append(
            CoherenceIssue(
                "WARNING",
                "ml_veto_no_model",
                f"{market}: ml_veto on but S4 model not ready",
            )
        )

    stop_pts = float(inst.get("stop_distance_points") or 0)
    if stop_pts <= 0:
        issues.append(
            CoherenceIssue(
                "WARNING",
                "stop_distance_missing",
                f"{market}: stop_distance_points not set",
            )
        )
    risk_cap = float(inst.get("risk_cap_gbp") or 0)
    if risk_cap <= 0:
        issues.append(
            CoherenceIssue(
                "WARNING",
                "risk_cap_missing",
                f"{market}: risk_cap_gbp not set",
            )
        )

    crit = [i for i in issues if i.severity == "CRITICAL"]
    return MarketCoherence(
        instrument_id=instrument_id,
        epic=epic,
        market=market,
        ok=not crit,
        current_session=current_session,
        session_allowed=session_ok,
        effective_confidence_threshold=round(eff_th, 1),
        config_signal_threshold=sig_th,
        points_state=points_state,
        fitness_floor_pct=fitness,
        ml_veto_active=ml_on,
        ml_veto_model_ready=ml_ready,
        stop_distance_pts=stop_pts,
        risk_cap_gbp=risk_cap,
        issues=issues,
    )


def _s4_veto_epics() -> set[str]:
    manifest = project_root() / "data_lake" / "models" / "s4" / "manifest.json"
    out: set[str] = set()
    if not manifest.is_file():
        return out
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for epic, info in (data.get("by_epic") or {}).items():
            if info.get("ok") and info.get("veto_eligible"):
                out.add(str(epic))
    except (json.JSONDecodeError, OSError):
        pass
    return out


def audit_trading_readiness(
    cfg: Any,
    store: Any | None = None,
    *,
    points_state: str | None = None,
    repair_db: bool = True,
    per_market: bool = True,
) -> CoherenceReport:
    """Run all coherence checks; optionally repair corrupt SQLite trade rows."""
    issues: list[CoherenceIssue] = []
    v26 = _load_v26()

    if store is not None and repair_db:
        try:
            n = repair_corrupt_trade_rows(store, cfg)
            if n:
                issues.append(
                    CoherenceIssue(
                        "INFO",
                        "db_repaired",
                        f"Repaired {n} corrupt trade row(s) (empty epic / stop=0)",
                        remedy="Rows updated at startup; monitor learning_db.sqlite3",
                    )
                )
        except Exception as e:
            issues.append(
                CoherenceIssue(
                    "WARNING",
                    "db_repair_failed",
                    f"Trade row repair failed: {type(e).__name__}: {e}",
                )
            )

    # --- Enabled instruments ---
    try:
        from trading.instrument_registry import InstrumentRegistry

        reg = InstrumentRegistry(
            cfg.as_dict() if hasattr(cfg, "as_dict") else dict(cfg)
        )
        enabled = reg.get_enabled_with_ids()
    except Exception as e:
        issues.append(
            CoherenceIssue(
                "CRITICAL",
                "instruments_unreadable",
                f"Cannot load enabled instruments: {e}",
            )
        )
        enabled = []

    for iid, inst in enabled:
        epic = str(inst.get("epic") or "").strip()
        if not epic:
            issues.append(
                CoherenceIssue(
                    "CRITICAL",
                    "instrument_missing_epic",
                    f"Instrument '{iid}' enabled but has no epic",
                    remedy="Fix config_v25.json instruments block",
                )
            )
        wl = list(inst.get("trading_session_whitelist") or [])
        if not wl:
            issues.append(
                CoherenceIssue(
                    "WARNING",
                    "instrument_no_session_whitelist",
                    f"{epic or iid}: no trading_session_whitelist — uses global default",
                )
            )

    # --- Points vs thresholds ---
    pts = (points_state or "HEALTHY").upper()
    relax = v26.get("gate_relaxations") or {}
    if pts == "WARNING":
        if not relax.get("enabled"):
            issues.append(
                CoherenceIssue(
                    "WARNING",
                    "points_warning_no_relaxation",
                    "Points WARNING raises bar to 92% but gate_relaxations disabled",
                    remedy="Enable gate_relaxations.warning_use_instrument_threshold in config_v26.json",
                )
            )
        elif not relax.get("warning_use_instrument_threshold"):
            issues.append(
                CoherenceIssue(
                    "WARNING",
                    "points_warning_92",
                    "Points WARNING active — confidence bar is 92% unless warning_use_instrument_threshold is set",
                    remedy="Set gate_relaxations.warning_use_instrument_threshold: true",
                )
            )
    if pts == "STOP":
        issues.append(
            CoherenceIssue(
                "CRITICAL",
                "points_stop",
                "Points state STOP — no trades until cumulative recovers",
                remedy="Run scripts/reset_points_healthy.py after reviewing losses",
            )
        )

    # --- Portfolio envelope sanity ---
    envelope = v26.get("capital_envelope") or {}
    max_daily = float(envelope.get("max_daily_risk_deployed_gbp") or 2500)
    gate_on = bool((v26.get("portfolio_gate") or {}).get("enabled"))
    if gate_on and store is not None:
        try:
            from execution.trade_risk import risk_gbp_from_row
            from system.portfolio_envelope import rehydrate, snapshot

            today = date.today().isoformat()
            daily = 0.0
            for row in store.conn.execute(
                """
                SELECT entry, stop, size, epic, dry_run
                FROM trades
                WHERE substr(opened_at, 1, 10) = ? AND dry_run = 0
                """,
                (today,),
            ):
                daily += risk_gbp_from_row(row, cfg=cfg)
            from system.daily_loss_policy import effective_daily_pnl

            rehydrate(
                concurrent_risk_gbp=0.0,
                daily_deployed_gbp=daily,
                daily_pnl_gbp=float(effective_daily_pnl(store, day=today)),
            )
            snap = snapshot()
            dep = float(snap.get("daily_deployed_gbp") or 0)
            if dep > max_daily:
                issues.append(
                    CoherenceIssue(
                        "CRITICAL",
                        "portfolio_daily_deploy_exceeded",
                        f"Rehydrated daily_deploy £{dep:.0f} > cap £{max_daily:.0f} — entries blocked",
                        remedy="Corrupt trade rows or stop=0 in SQLite; restart after repair_corrupt_trade_rows",
                    )
                )
            elif dep > max_daily * 0.85:
                issues.append(
                    CoherenceIssue(
                        "WARNING",
                        "portfolio_daily_deploy_high",
                        f"Daily deploy £{dep:.0f} is {100 * dep / max_daily:.0f}% of £{max_daily:.0f} cap",
                    )
                )
        except Exception as e:
            issues.append(
                CoherenceIssue(
                    "WARNING",
                    "portfolio_audit_failed",
                    f"Portfolio envelope audit failed: {type(e).__name__}: {e}",
                )
            )

    # --- ml_veto vs S4 models ---
    ml = v26.get("ml_veto") or {}
    if ml.get("enabled"):
        per = ml.get("per_epic") or {}
        manifest = project_root() / "data_lake" / "models" / "s4" / "manifest.json"
        s4_epics: set[str] = set()
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                for epic, info in (data.get("by_epic") or {}).items():
                    if info.get("ok") and info.get("veto_eligible"):
                        s4_epics.add(str(epic))
            except (json.JSONDecodeError, OSError):
                pass
        for epic, block in per.items():
            if not (isinstance(block, dict) and block.get("enabled")):
                continue
            if epic not in s4_epics:
                issues.append(
                    CoherenceIssue(
                        "WARNING",
                        "ml_veto_no_model",
                        f"ml_veto enabled for {epic} but no veto-eligible S4 model on disk",
                        remedy="Run scripts/v26_learning_pack.py (OHLC + S4 retrain)",
                    )
                )

    # --- Per-market alignment ---
    markets: list[MarketCoherence] = []
    now_sess = ""
    try:
        from signals.indicators import session_name

        now_sess = session_name()
    except Exception:
        now_sess = "unknown"

    pts_state = (points_state or "HEALTHY").upper()
    s4_epics = _s4_veto_epics()
    if per_market:
        for iid, inst in enabled:
            mc = audit_market(
                instrument_id=iid,
                inst=inst,
                cfg=cfg,
                points_state=pts_state,
                v26=v26,
                current_session=now_sess,
                s4_veto_epics=s4_epics,
            )
            markets.append(mc)
            for item in mc.issues:
                if item.severity == "CRITICAL":
                    issues.append(
                        CoherenceIssue(
                            item.severity,
                            f"{mc.epic}:{item.code}",
                            item.message,
                            remedy=item.remedy,
                        )
                    )
                elif item.severity == "WARNING":
                    issues.append(
                        CoherenceIssue(
                            item.severity,
                            f"{mc.epic}:{item.code}",
                            item.message,
                            remedy=item.remedy,
                        )
                    )

    tradeable_now = sum(1 for m in markets if m.session_allowed)
    if enabled and tradeable_now == 0:
        issues.append(
            CoherenceIssue(
                "INFO",
                "session_quiet",
                f"No enabled instrument allows session '{now_sess}' — expected outside core windows",
                remedy="Normal off-hours; DOW/Gold need us_afternoon, FX needs london_*",
            )
        )

    from datetime import datetime, timezone

    critical = [i for i in issues if i.severity == "CRITICAL"]
    market_ok = all(m.ok for m in markets) if markets else True
    return CoherenceReport(
        ok=not critical and market_ok,
        issues=issues,
        markets=markets,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def report_to_dict(report: CoherenceReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "generated_at": report.generated_at,
        "issues": [
            {
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "remedy": i.remedy,
            }
            for i in report.issues
        ],
        "markets": [
            {
                "instrument_id": m.instrument_id,
                "epic": m.epic,
                "market": m.market,
                "ok": m.ok,
                "current_session": m.current_session,
                "session_allowed": m.session_allowed,
                "effective_confidence_threshold": m.effective_confidence_threshold,
                "config_signal_threshold": m.config_signal_threshold,
                "points_state": m.points_state,
                "fitness_floor_pct": m.fitness_floor_pct,
                "ml_veto_active": m.ml_veto_active,
                "ml_veto_model_ready": m.ml_veto_model_ready,
                "stop_distance_pts": m.stop_distance_pts,
                "risk_cap_gbp": m.risk_cap_gbp,
                "issues": [
                    {"severity": x.severity, "code": x.code, "message": x.message}
                    for x in m.issues
                ],
            }
            for m in report.markets
        ],
    }


def write_coherence_snapshot(report: CoherenceReport) -> Path:
    out = project_root() / "data_lake" / "state" / "gate_coherence_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report_to_dict(report), indent=2),
        encoding="utf-8",
    )
    return out


def format_report(report: CoherenceReport) -> str:
    lines = ["Gate coherence audit", "=" * 40]
    if report.markets:
        lines.append("\nPer market:")
        for m in report.markets:
            flag = "OPEN" if m.session_allowed else "closed"
            lines.append(
                f"  {m.market} ({m.epic}): {flag} | "
                f"thresh {m.effective_confidence_threshold:.0f}% "
                f"(cfg {m.config_signal_threshold:.0f}%) | "
                f"fitness ≥{m.fitness_floor_pct:.0f}% | "
                f"ml_veto={'on' if m.ml_veto_active else 'off'}"
                + (
                    f" ({'ready' if m.ml_veto_model_ready else 'no model'})"
                    if m.ml_veto_active
                    else ""
                )
            )
            for item in m.issues:
                if item.severity in ("CRITICAL", "WARNING"):
                    lines.append(f"    ! [{item.code}] {item.message}")
    for sev in ("CRITICAL", "WARNING", "INFO"):
        bucket = [i for i in report.issues if i.severity == sev and ":" not in i.code]
        if not bucket:
            continue
        lines.append(f"\n{sev} (global):")
        for item in bucket:
            lines.append(f"  [{item.code}] {item.message}")
            if item.remedy:
                lines.append(f"    → {item.remedy}")
    lines.append("")
    lines.append("RESULT: " + ("OK" if report.ok else "BLOCKED — fix CRITICAL items"))
    return "\n".join(lines)


def run_scheduled_coherence_check(
    *,
    repair_db: bool = False,
    alert_on_critical: bool = True,
) -> CoherenceReport:
    """Full alignment check — used by launchd (4×/day) and in-agent scheduler."""
    from data.learning_store import LearningStore
    from system.config_loader import ConfigLoader
    from system.engine_log import log_engine
    from system.paths import data_dir
    from trading.points_engine import PointsEngine

    cfg = ConfigLoader().load()
    store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
    store.connect()
    points = PointsEngine(store)
    report = audit_trading_readiness(
        cfg,
        store,
        points_state=points.get_state(),
        repair_db=repair_db,
        per_market=True,
    )
    path = write_coherence_snapshot(report)
    store.close()
    tradeable = sum(1 for m in report.markets if m.session_allowed)
    log_engine(
        f"gate_coherence scheduled: ok={report.ok} markets={len(report.markets)} "
        f"tradeable_now={tradeable} snapshot={path.name}"
    )
    if alert_on_critical and report.critical:
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(
                "Gate coherence CRITICAL: " + report.critical[0].message[:100]
            )
        except Exception:
            pass
    return report
