"""
One-time IG transaction history bootstrap — in-memory ledger cache for dashboard.

Fetches GET /history/transactions/ALL/{from}/{to} exactly once after authentication,
caches the last 5 CFD contracts (24h window) for sub-2ms telemetry reads.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

CFD_ACCOUNT_ID = "Z6BAH4"
_HYDRATE_LIMIT = 5
_HYDRATE_HOURS = 24.0

_lock = threading.Lock()
_bootstrap_complete = False
_hydrated_at: float = 0.0
_ledger_cache: list[dict[str, Any]] = []
_bootstrap_error: str = ""


def _txn_to_ledger_row(txn: dict[str, Any]) -> dict[str, Any] | None:
    from system.ig_transactions import parse_ig_transaction_row

    row = parse_ig_transaction_row(txn)
    if not row:
        return None
    epic = str(row.get("epic") or row.get("instrument") or "").strip()
    deal_id = str(row.get("deal_id") or row.get("dealId") or row.get("reference") or "").strip()
    if not epic and not deal_id:
        return None
    pnl = float(row.get("pnl") or row.get("profitAndLoss") or row.get("profit") or 0.0)
    ts = str(row.get("closed_at") or row.get("date") or row.get("timestamp") or "")
    status = str(row.get("status") or row.get("transactionStatus") or "CLOSED").upper()
    terminal = status in ("CLOSED", "REJECTED", "FAILED", "DELETED")
    return {
        "timestamp": ts,
        "opened_at": ts,
        "epic": epic,
        "direction": str(row.get("direction") or "").upper(),
        "entry": row.get("open_level") or row.get("openLevel") or row.get("level"),
        "dealId": deal_id or epic,
        "deal_id": deal_id or epic,
        "deal_reference": deal_id,
        "size": float(row.get("size") or row.get("quantity") or 0.0),
        "pnl": pnl,
        "pnl_gbp": pnl,
        "status": status,
        "terminal": terminal,
        "source": "ig_hydration_cache",
        "trail_progress_pct": 100.0 if terminal else 0.0,
        "points_to_trail": 0.0,
        "account_id": CFD_ACCOUNT_ID,
    }


def bootstrap_ledger_history_once(rest_client: Any | None) -> dict[str, Any]:
    """
    One-shot REST hydration — /history/transactions/ALL over 24h, top 5 rows cached.
    Idempotent; safe to call from post-ready or background thread.
    """
    global _bootstrap_complete, _hydrated_at, _ledger_cache, _bootstrap_error

    with _lock:
        if _bootstrap_complete:
            return ledger_hydration_state()

    if rest_client is None:
        with _lock:
            _bootstrap_error = "rest_client_unavailable"
            _bootstrap_complete = True
        return ledger_hydration_state()

    rows: list[dict[str, Any]] = []
    err = ""
    try:
        txns: list[dict[str, Any]] = []
        for txn_type in ("ALL", "ALL_DEAL"):
            if hasattr(rest_client, "fetch_transaction_history"):
                txns = list(
                    rest_client.fetch_transaction_history(
                        hours=_HYDRATE_HOURS,
                        transaction_type=txn_type,
                        page_size=500,
                    )
                    or []
                )
            elif hasattr(rest_client, "fetch_transactions"):
                from system.ig_transactions import ig_date_range_dd_mm_yyyy

                start, end = ig_date_range_dd_mm_yyyy(days_back=1)
                txns = list(
                    rest_client.fetch_transactions(
                        start, end, transaction_type=txn_type, page_size=500
                    )
                    or []
                )
            if txns:
                break

        for txn in txns[: _HYDRATE_LIMIT * 3]:
            if not isinstance(txn, dict):
                continue
            row = _txn_to_ledger_row(txn)
            if row:
                rows.append(row)
            if len(rows) >= _HYDRATE_LIMIT:
                break
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        log_engine(f"LedgerHydration: bootstrap failed {err}")

    with _lock:
        _ledger_cache = rows[:_HYDRATE_LIMIT]
        _hydrated_at = time.time()
        _bootstrap_complete = True
        _bootstrap_error = err

    log_engine(
        f"LedgerHydration: one-time sync complete account={CFD_ACCOUNT_ID} "
        f"rows={len(_ledger_cache)} error={err or 'none'}"
    )
    return ledger_hydration_state()


def get_cached_ledger_rows() -> list[dict[str, Any]]:
    """Sub-2ms memory read for /api/v31/telemetry hot path."""
    with _lock:
        return [dict(r) for r in _ledger_cache]


def ledger_cache_ready() -> bool:
    with _lock:
        return bool(_ledger_cache)


def ledger_hydration_state() -> dict[str, Any]:
    with _lock:
        return {
            "ledger_hydrated": _bootstrap_complete,
            "ledger_synced": bool(_ledger_cache) and not _bootstrap_error,
            "ledger_hydration_source": "ig_history_transactions_cache",
            "ledger_hydration_account": CFD_ACCOUNT_ID,
            "ledger_row_count": len(_ledger_cache),
            "ledger_hydrated_at": _hydrated_at,
            "ledger_hydration_error": _bootstrap_error,
        }


def reset_ledger_hydration_for_tests() -> None:
    global _bootstrap_complete, _hydrated_at, _ledger_cache, _bootstrap_error
    with _lock:
        _bootstrap_complete = False
        _hydrated_at = 0.0
        _ledger_cache = []
        _bootstrap_error = ""
