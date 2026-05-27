"""
Closed-trades display policy for DEMO/LIVE — IG-first, no SIM pollution.
"""

from __future__ import annotations

from typing import Any

from system.closed_trades_reconcile import is_broker_aligned_row
from system.degraded_mode import is_degraded

# Permanent display exclusions — soak/proof/replay must never appear in main GUI.
EXCLUDED_SOURCES = ("sim", "soak", "proof", "replay", "test")

_excluded_startup_logged = False


def filter_broker_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if is_broker_aligned_row(r)]


def is_simulator_row(row: dict[str, Any]) -> bool:
    ref = str(row.get("deal_reference") or row.get("ig_deal_id") or "").strip()
    if ref.startswith("SIM-"):
        return True
    if str(row.get("source") or "") in ("sim", "simulator", "test"):
        return True
    return bool(row.get("dry_run")) and not is_broker_aligned_row(row)


def is_excluded_display_row(row: dict[str, Any]) -> bool:
    """Exclude SIM/soak/proof/replay rows from main closed-trades display."""
    if is_simulator_row(row):
        return True
    src = str(row.get("source") or "").lower()
    setup = str(row.get("setup_key") or "").lower()
    ref = str(row.get("deal_reference") or row.get("ig_deal_id") or "").upper()
    if ref.startswith("SIM-"):
        return True
    return any(tag in src or tag in setup for tag in EXCLUDED_SOURCES)


def _filter_excluded_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global _excluded_startup_logged
    kept: list[dict[str, Any]] = []
    excluded = 0
    for row in rows:
        if is_excluded_display_row(row):
            excluded += 1
            continue
        kept.append(row)
    if excluded and not _excluded_startup_logged:
        from system.engine_log import log_engine

        log_engine(f"Closed trades: excluded {excluded} SIM/soak rows from display")
        _excluded_startup_logged = True
    return kept


def closed_trades_source_label(
    *,
    ig_row_count: int,
    txn_error: str = "",
    txn_stale: bool = False,
    broker_only: bool = False,
    using_local_fallback: bool = False,
) -> str:
    if ig_row_count > 0:
        if txn_stale:
            return "IG History (cached)"
        return "IG History"
    if is_degraded():
        return "IG History unavailable (rate limit)"
    if txn_error:
        return f"IG sync error ({txn_error[:40]})"
    if broker_only and using_local_fallback:
        return "IG position sync (history pending)"
    if broker_only:
        return "No IG closed deals in window"
    return "local store"


def merge_for_display(
    ig_rows: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
    *,
    limit: int,
    broker_only: bool,
) -> tuple[list[dict[str, Any]], str]:
    """Return merged rows and source hint for UI."""
    from system.closed_trades_merger import merge_closed_trades

    ig_rows = _filter_excluded_rows(ig_rows)
    local_rows = _filter_excluded_rows(local_rows)

    if is_degraded():
        if ig_rows:
            out = ig_rows[:limit]
            return out, closed_trades_source_label(
                ig_row_count=len(out), txn_stale=True, broker_only=broker_only
            )
        return [], closed_trades_source_label(ig_row_count=0, broker_only=broker_only)

    if broker_only:
        local_filtered = filter_broker_rows(local_rows)
        if ig_rows:
            merged, _ = merge_closed_trades(ig_rows, local_filtered, limit=limit)
            return merged, closed_trades_source_label(ig_row_count=len(ig_rows), broker_only=True)
        if local_filtered:
            return local_filtered[:limit], closed_trades_source_label(
                ig_row_count=0, broker_only=True, using_local_fallback=True
            )
        return [], closed_trades_source_label(ig_row_count=0, broker_only=True)

    merged, label = merge_closed_trades(ig_rows, local_rows, limit=limit)
    return merged, label
