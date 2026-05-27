"""
Reconcile closed trades: IG transaction history vs local learning store / UI cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def trade_key(row: dict[str, Any]) -> str:
    ref = str(row.get("deal_reference") or "").strip()
    ig = str(row.get("ig_deal_id") or "").strip()
    if ref and not ref.endswith(("TYNK", "TYPT")):
        return ref
    return ig or ref


def index_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = trade_key(r)
        if k:
            out[k] = r
    return out


@dataclass
class FieldMismatch:
    deal_reference: str
    field: str
    ig_value: Any
    local_value: Any


@dataclass
class ReconcileReport:
    ok: bool
    hours_window: float
    ig_count: int
    local_count: int
    matched: int
    mismatches: list[FieldMismatch] = field(default_factory=list)
    only_ig: list[str] = field(default_factory=list)
    only_local: list[str] = field(default_factory=list)
    ig_total_pnl: float = 0.0
    local_total_pnl: float = 0.0
    notes: list[str] = field(default_factory=list)
    mode: str = "full"
    session_deal_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "hours_window": self.hours_window,
            "session_deal_ids": self.session_deal_ids,
            "ig_count": self.ig_count,
            "local_count": self.local_count,
            "matched": self.matched,
            "mismatches": [
                {
                    "deal_reference": m.deal_reference,
                    "field": m.field,
                    "ig": m.ig_value,
                    "local": m.local_value,
                }
                for m in self.mismatches
            ],
            "only_ig": self.only_ig,
            "only_local": self.only_local,
            "ig_total_pnl": round(self.ig_total_pnl, 2),
            "local_total_pnl": round(self.local_total_pnl, 2),
            "pnl_delta": round(self.ig_total_pnl - self.local_total_pnl, 2),
            "notes": self.notes,
        }


def _pnl(row: dict[str, Any]) -> float:
    if row.get("ig_pnl_currency") is not None:
        return float(row["ig_pnl_currency"])
    return float(row.get("pnl_points") or 0)


def _close_float(a: object, b: object, tol: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a).strip().upper() == str(b).strip().upper()


def is_broker_aligned_row(row: dict[str, Any]) -> bool:
    """Rows that came from IG sync or carry an IG deal id."""
    if str(row.get("ig_deal_id") or "").strip():
        return True
    notes = str(row.get("notes") or "")
    if "IG sync" in notes or "IG transaction" in notes:
        return True
    ref = str(row.get("deal_reference") or "").strip()
    return ref.startswith("DIAAA")


def filter_local_rows_for_window(
    local_rows: list[dict[str, Any]],
    *,
    hours: float,
    epic: str = "",
    broker_only: bool = True,
) -> list[dict[str, Any]]:
    from system.ig_transactions import filter_rows_last_hours

    rows = filter_rows_last_hours(local_rows, hours)
    if epic:
        rows = [r for r in rows if not r.get("epic") or r.get("epic") == epic]
    if broker_only:
        rows = [r for r in rows if is_broker_aligned_row(r)]
    return rows


def reconcile_closed_trades(
    ig_rows: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
    *,
    hours_window: float = 168.0,
    pnl_tolerance: float = 0.05,
    level_tolerance: float = 25.0,
    session_only: bool = False,
    session_deal_ids: list[str] | None = None,
) -> ReconcileReport:
    """
    Compare IG-authoritative rows with local DB rows keyed by deal reference.
    ``ok`` when every IG deal in the window exists locally with matching fields.
    """
    if session_only and session_deal_ids:
        want = {str(d).strip() for d in session_deal_ids if d}
        ig_rows = [r for r in ig_rows if trade_key(r) in want]
        local_rows = [r for r in local_rows if trade_key(r) in want]

    ig_map = index_by_key(ig_rows)
    local_map = index_by_key(local_rows)
    ig_keys = set(ig_map)
    local_keys = set(local_map)

    mismatches: list[FieldMismatch] = []
    matched = 0

    for ref in sorted(ig_keys):
        ig = ig_map[ref]
        loc = local_map.get(ref)
        if not loc:
            continue
        matched += 1
        for fld, ig_val, loc_val, tol in (
            ("side", ig.get("side"), loc.get("side"), 0),
            ("entry", ig.get("entry"), loc.get("entry"), level_tolerance),
            ("exit", ig.get("exit"), loc.get("exit"), level_tolerance),
            ("ig_pnl_currency", _pnl(ig), _pnl(loc), pnl_tolerance),
        ):
            if fld == "side":
                if str(ig_val or "").upper() != str(loc_val or "").upper():
                    mismatches.append(FieldMismatch(ref, fld, ig_val, loc_val))
            elif not _close_float(ig_val, loc_val, tol):
                mismatches.append(FieldMismatch(ref, fld, ig_val, loc_val))

    only_ig = sorted(ig_keys - local_keys)
    only_local = sorted(local_keys - ig_keys)

    ig_total = sum(_pnl(ig_map[k]) for k in ig_keys)
    local_total = sum(_pnl(local_map[k]) for k in local_keys)

    notes: list[str] = []
    if not ig_rows:
        notes.append("No IG closed deals in window — run DEMO trades or widen history_days.")
        if local_rows and abs(local_total) > 500:
            notes.append(
                f"Local store has {len(local_rows)} row(s) (Σ P/L {local_total:+.2f}) but IG shows none — "
                "likely TEST/simulator history; UI uses IG rows when available."
            )
    if only_local:
        notes.append(
            f"{len(only_local)} local-only row(s): simulator or pre-sync closes not in IG history."
        )
    if only_ig:
        notes.append(
            f"{len(only_ig)} IG row(s) missing locally — run app in DEMO to ingest transactions."
        )

    only_local_in_window = [k for k in only_local if k in local_keys]
    mode = "session" if session_only else "full"
    if session_only:
        want = {str(d).strip() for d in (session_deal_ids or []) if d}
        session_in_ig = sorted(want & ig_keys)
        session_in_local = sorted(want & local_keys)
        missing_ig = sorted(want - ig_keys)
        missing_local = sorted(want - local_keys)
        ok = not mismatches and not missing_ig and not missing_local
        if want:
            notes.append(
                f"Session scope: {len(want)} deal ID(s); "
                f"IG {len(session_in_ig)} | local {len(session_in_local)} | matched {matched}."
            )
        if missing_ig:
            notes.append(f"Session deals missing from IG history: {', '.join(missing_ig[:6])}")
        if missing_local:
            notes.append(f"Session deals missing from agent DB: {', '.join(missing_local[:6])}")
        only_local = [k for k in only_local if k in want] if want else only_local
    else:
        ok = not mismatches and not only_local
        if ig_rows and not mismatches and only_ig and not only_local:
            ok = True
            notes.append(
                f"{len(only_ig)} IG deal(s) not yet in local DB — run DEMO to ingest transactions."
            )
        elif ig_rows and not mismatches and not only_ig:
            ok = True
            if only_local_in_window:
                notes.append(
                    f"{len(only_local_in_window)} local-only row(s) in window "
                    "(acceptable if position-sync ahead of IG history)."
                )

    return ReconcileReport(
        ok=ok,
        hours_window=hours_window,
        ig_count=len(ig_rows),
        local_count=len(local_rows),
        matched=matched,
        mismatches=mismatches,
        only_ig=only_ig,
        only_local=only_local,
        ig_total_pnl=ig_total,
        local_total_pnl=local_total,
        notes=notes,
        mode=mode,
        session_deal_ids=list(session_deal_ids or []),
    )


def fetch_ig_closed_rows(
    rest_client: Any,
    *,
    epic: str,
    history_days: int = 2,
    display_hours: float = 24.0,
) -> list[dict[str, Any]]:
    from system.ig_transactions import (
        build_activity_time_lookup,
        filter_rows_last_hours,
        ig_date_range_dd_mm_yyyy,
        parse_ig_transaction_row,
    )

    start, end = ig_date_range_dd_mm_yyyy(days_back=history_days)
    txns = rest_client.fetch_transactions(
        start,
        end,
        transaction_type="ALL_DEAL",
        page_size=500,
    )
    activity_times: dict[str, str] = {}
    if hasattr(rest_client, "fetch_account_activity"):
        try:
            activities = rest_client.fetch_account_activity(start, end)
            activity_times = build_activity_time_lookup(activities)
        except Exception:
            activity_times = {}
    rows: list[dict[str, Any]] = []
    for txn in txns:
        row = parse_ig_transaction_row(txn, epic_filter=epic, activity_times=activity_times)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
    return filter_rows_last_hours(rows, display_hours)


def format_report_text(report: ReconcileReport) -> str:
    title = (
        "=== Session reconcile (IG vs agent) ==="
        if report.mode == "session"
        else "=== Closed trades reconcile (IG vs local) ==="
    )
    lines = [
        title,
        f"Window: last {report.hours_window:.0f}h",
        f"IG deals: {report.ig_count} | Local: {report.local_count} | Matched: {report.matched}",
        f"IG total P/L: {report.ig_total_pnl:+.2f} | Local: {report.local_total_pnl:+.2f} | "
        f"Δ {report.ig_total_pnl - report.local_total_pnl:+.2f}",
        f"Status: {'PASS — aligned with IG' if report.ok else 'FAIL — review mismatches'}",
    ]
    if report.only_ig:
        lines.append(f"Missing locally ({len(report.only_ig)}): " + ", ".join(report.only_ig[:8]))
        if len(report.only_ig) > 8:
            lines.append(f"  … +{len(report.only_ig) - 8} more")
    if report.only_local:
        lines.append(f"Local only ({len(report.only_local)}): " + ", ".join(report.only_local[:8]))
    for m in report.mismatches[:12]:
        lines.append(f"  MISMATCH {m.deal_reference[:12]} {m.field}: IG={m.ig_value} local={m.local_value}")
    for n in report.notes:
        lines.append(f"  Note: {n}")
    return "\n".join(lines)


def run_reconcile_from_clients(
    rest_client: Any,
    store: Any,
    *,
    epic: str,
    history_days: int = 2,
    display_hours: float = 24.0,
    pnl_tolerance: float = 0.05,
    local_limit: int = 500,
    session_only: bool = False,
    session_deal_ids: list[str] | None = None,
) -> ReconcileReport:
    ig_rows = fetch_ig_closed_rows(
        rest_client,
        epic=epic,
        history_days=history_days,
        display_hours=display_hours,
    )
    local_all = store.recent_closed_trades(local_limit) if store else []
    local_rows = filter_local_rows_for_window(local_all, hours=display_hours, epic=epic)
    ids = session_deal_ids
    if session_only and not ids and store:
        from system.closed_trades_merger import recent_deal_ids

        ids = recent_deal_ids(local_rows, limit=12)
    return reconcile_closed_trades(
        ig_rows,
        local_rows,
        hours_window=display_hours,
        pnl_tolerance=pnl_tolerance,
        session_only=session_only,
        session_deal_ids=ids,
    )
