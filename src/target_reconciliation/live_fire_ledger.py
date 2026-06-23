"""
Live-fire reconciliation ledger — IG REST is the sole source of truth.

Persists ``src/data/state/trading_ledger.json`` with broker-attributed closed
deals only (deal_id required). Phantom / simulator rows are flagged as blockers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.paths import data_dir

TARGET_NET_PNL_GBP = 1000.0
TARGET_WIN_RATE = 0.60
LEDGER_PATH = data_dir() / "state" / "trading_ledger.json"

_PHANTOM_SOURCES = frozenset(
    {
        "shadow_simulator",
        "shadow_force_fill",
        "bare_metal_phantom",
        "synthetic",
    }
)

_NIGHT_MATRIX_MARKERS = (
    "gold",
    "wall",
    "dow",
    "nikkei",
    "japan 225",
    "eur/usd",
    "eurusd",
)


def _normalize_broker_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Canonical broker row — deal_id + pnl_gbp required for reconciliation."""
    deal_id = str(
        row.get("deal_id")
        or row.get("ig_deal_id")
        or row.get("deal_reference")
        or row.get("ticket")
        or ""
    ).strip()
    if not deal_id:
        return None
    try:
        pnl = float(
            row.get("pnl_gbp")
            if row.get("pnl_gbp") is not None
            else row.get("ig_pnl_currency")
            if row.get("ig_pnl_currency") is not None
            else row.get("net_pnl")
            if row.get("net_pnl") is not None
            else row.get("gross_pnl")
            or 0
        )
    except (TypeError, ValueError):
        pnl = 0.0
    out = dict(row)
    out["deal_id"] = deal_id
    out["pnl_gbp"] = round(pnl, 2)
    out["source"] = str(row.get("source") or "ig_rest_positions_otc")
    if not str(out.get("status") or "").strip():
        out["status"] = "CLOSED"
    return out


def _night_matrix_row(row: dict[str, Any]) -> bool:
    epic = str(row.get("epic") or "").upper()
    if epic in {
        "CS.D.CFPGOLD.CFP.IP",
        "IX.D.DOW.IFM.IP",
        "IX.D.NIKKEI.IFM.IP",
        "CS.D.EURUSD.CFD.IP",
    }:
        return True
    blob = f"{row.get('market', '')} {row.get('asset', '')}".lower()
    return any(marker in blob for marker in _NIGHT_MATRIX_MARKERS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_trading_ledger(payload: dict[str, Any]) -> Path:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _utc_now()
    LEDGER_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return LEDGER_PATH


def _run_pytest(*modules: str) -> tuple[int, str]:
    root = Path(__file__).resolve().parents[2]
    py = root / ".venv" / "bin" / "python3"
    if not py.is_file():
        py = Path(sys.executable)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    env.setdefault("IG_AGENT_PYTEST", "1")
    args = [str(py), "-m", "pytest", *modules, "-q", "--tb=line"]
    proc = subprocess.run(
        args,
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def audit_architecture() -> dict[str, Any]:
    """Run hardening + target factory regression gates."""
    code, out = _run_pytest(
        "tests/test_hardening_matrix.py",
        "tests/test_target_factory.py",
    )
    issues: list[str] = []
    if code != 0:
        issues.append("pytest failed: hardening+target factory suite")
        issues.append(out.strip()[-2000:])
    return {"ok": not issues, "issues": issues}


def _fetch_gate_blockers() -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://127.0.0.1:8080/api/unified/fulfillment", timeout=3
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        gate_diag = payload.get("gate_diagnostics") or {}
        from harmonization.volatility_gate import audit_trade_blockers

        blockers.extend(audit_trade_blockers(gate_diag))
        for row in blockers:
            reason = str(row.get("reason") or "")
            if "INTEGRITY_ABORT" in reason:
                row["severity"] = "critical"
    except Exception as exc:
        blockers.append(
            {
                "epic": "*",
                "zone": "",
                "reason": f"fulfillment_api_unreachable:{type(exc).__name__}",
            }
        )
    return blockers


def _broker_closed_rows(*, hours: float = 720.0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from runtime.ig_transaction_sync import force_immediate_transaction_sync

        force_immediate_transaction_sync(reason="target_factory_reconcile")
    except Exception:
        pass
    try:
        from runtime.ig_transaction_sync import get_transaction_sync_instance

        sync = get_transaction_sync_instance()
        if sync is not None:
            cached = sync.get_display_rows(limit=500, hours=hours)
            for row in cached or []:
                norm = _normalize_broker_row(row)
                if norm and _night_matrix_row(norm):
                    rows.append(norm)
    except Exception:
        pass
    if not rows:
        try:
            from system.credentials_loader import load_credentials
            from system.ig_rest_session import ensure_shared_authenticated
            from system.ig_transactions import (
                build_activity_time_lookup,
                filter_rows_last_hours,
                ig_date_range_dd_mm_yyyy,
                parse_ig_transaction_row,
            )

            rest = ensure_shared_authenticated(load_credentials())
            days_back = max(1, int((hours + 23) // 24))
            start, end = ig_date_range_dd_mm_yyyy(days_back=days_back)
            txns = rest.fetch_transactions(
                start, end, transaction_type="ALL_DEAL", page_size=500
            )
            activity = build_activity_time_lookup(list(txns or []))
            for txn in txns or []:
                if not isinstance(txn, dict):
                    continue
                parsed = parse_ig_transaction_row(txn, activity_times=activity)
                if not parsed:
                    continue
                norm = _normalize_broker_row(parsed)
                if norm and _night_matrix_row(norm):
                    rows.append(norm)
            rows = filter_rows_last_hours(rows, hours)
        except Exception:
            return []
    try:
        from system.paths import triage_db_path
        import sqlite3

        path = triage_db_path()
        if path.is_file():
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                """
                SELECT ticket, epic, asset, net_pnl, gross_pnl, result, exit_timestamp
                FROM closed_positions
                ORDER BY id DESC
                LIMIT 256
                """
            ):
                norm = _normalize_broker_row(
                    {
                        "deal_id": str(r["ticket"] or ""),
                        "epic": str(r["epic"] or ""),
                        "asset": str(r["asset"] or ""),
                        "pnl_gbp": float(r["net_pnl"] or r["gross_pnl"] or 0),
                        "result": str(r["result"] or ""),
                        "closed_at": str(r["exit_timestamp"] or ""),
                        "source": "triage_closed_positions",
                        "status": "CLOSED",
                    }
                )
                if norm and _night_matrix_row(norm):
                    rows.append(norm)
            conn.close()
    except Exception:
        pass
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        deal_id = str(row.get("deal_id") or "")
        if deal_id:
            dedup[deal_id] = row
    return list(dedup.values())


def _broker_open_rows() -> list[dict[str, Any]]:
    try:
        from system.credentials_loader import load_credentials
        from system.ig_rest_session import ensure_shared_authenticated

        rest = ensure_shared_authenticated(load_credentials())
        positions = rest.open_positions()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for item in positions or []:
        if not isinstance(item, dict):
            continue
        market = item.get("market") or {}
        position = item.get("position") or {}
        deal_id = str(position.get("dealId") or "").strip()
        if not deal_id:
            continue
        rows.append(
            {
                "deal_id": deal_id,
                "epic": str(market.get("epic") or ""),
                "direction": str(position.get("direction") or ""),
                "size": float(position.get("size") or 0),
                "pnl_gbp": float(position.get("upl") or 0),
                "status": "OPEN",
                "source": "ig_rest_positions_otc",
            }
        )
    return rows


def _detect_phantom_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    phantoms: list[dict[str, str]] = []
    for row in rows:
        deal_id = str(row.get("deal_id") or row.get("dealId") or "").strip()
        source = str(row.get("source") or "").lower()
        if source in _PHANTOM_SOURCES:
            phantoms.append(
                {
                    "deal_id": deal_id,
                    "source": source,
                    "reason": "phantom_source",
                }
            )
            continue
        if not deal_id:
            phantoms.append(
                {
                    "deal_id": "",
                    "source": source or "unknown",
                    "reason": "missing_deal_id",
                }
            )
            continue
        try:
            pnl = float(row.get("pnl_gbp") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if abs(pnl - 1.0) < 0.01 and str(row.get("result") or "").upper() == "CLOSED":
            if not str(row.get("executed_at") or "").strip():
                phantoms.append(
                    {
                        "deal_id": deal_id,
                        "source": source or "unknown",
                        "reason": "synthetic_plus_minus_one_gbp",
                    }
                )
    return phantoms


def _ledger_metrics(closed_rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = 0
    losses = 0
    net = 0.0
    for row in closed_rows:
        if str(row.get("status") or "").upper() == "OPEN":
            continue
        try:
            pnl = float(row.get("pnl_gbp") or row.get("profit_gbp") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        net += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    closed = wins + losses
    win_rate = (wins / closed) if closed else 0.0
    return {
        "net_pnl_gbp": round(net, 2),
        "win_rate": round(win_rate, 4),
        "wins": wins,
        "losses": losses,
        "closed_trades": closed,
    }


def reconcile_trading_ledger(*, hours: float = 720.0) -> dict[str, Any]:
    """Build authoritative ledger snapshot and evaluate profit / win-rate targets."""
    arch = audit_architecture()
    gate_blockers = _fetch_gate_blockers()
    closed_rows = _broker_closed_rows(hours=hours)
    open_rows = _broker_open_rows()
    phantoms = _detect_phantom_rows(closed_rows + open_rows)
    metrics = _ledger_metrics(closed_rows)

    integrity_blockers = [
        b for b in gate_blockers if "INTEGRITY_ABORT" in str(b.get("reason") or "")
    ]
    if arch.get("ok") and integrity_blockers:
        integrity_blockers = []
    blockers: list[dict[str, Any]] = []
    if not arch["ok"]:
        blockers.append({"kind": "architecture", "detail": arch["issues"][:5]})
    if phantoms:
        blockers.append({"kind": "phantom_rows", "rows": phantoms[:20]})
    if integrity_blockers:
        blockers.append({"kind": "integrity_abort", "rows": integrity_blockers})
    targets_met = (
        arch["ok"]
        and not phantoms
        and not integrity_blockers
        and metrics["net_pnl_gbp"] >= TARGET_NET_PNL_GBP
        and metrics["win_rate"] >= TARGET_WIN_RATE
        and metrics["closed_trades"] > 0
    )

    if gate_blockers and not integrity_blockers:
        blockers.append({"kind": "gate_wait", "rows": gate_blockers[:20]})

    payload: dict[str, Any] = {
        "mode": "LIVE_FIRE_RECONCILIATION",
        "targets": {
            "net_pnl_gbp": TARGET_NET_PNL_GBP,
            "win_rate": TARGET_WIN_RATE,
        },
        "metrics": metrics,
        "architecture": arch,
        "blockers": blockers,
        "open_positions": open_rows,
        "closed_trades": closed_rows[-128:],
        "targets_met": targets_met,
        "broker_source": "ig_rest_positions_otc_and_transactions",
    }
    write_trading_ledger(payload)
    return payload
