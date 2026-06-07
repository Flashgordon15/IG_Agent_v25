"""
Closed-trades display policy for DEMO/LIVE — IG-first, no SIM pollution.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
            return merged, closed_trades_source_label(
                ig_row_count=len(ig_rows), broker_only=True
            )
        if local_filtered:
            return local_filtered[:limit], closed_trades_source_label(
                ig_row_count=0, broker_only=True, using_local_fallback=True
            )
        return [], closed_trades_source_label(ig_row_count=0, broker_only=True)

    merged, label = merge_closed_trades(ig_rows, local_rows, limit=limit)
    return merged, label


_IG_IMPORT_SETUPS = frozenset({"ig|imported", "ig_import", "ig|import"})


def is_ig_import_row(row: dict[str, Any]) -> bool:
    setup = str(row.get("setup_key") or row.get("setup") or "").lower()
    src = str(row.get("source") or "").lower()
    return setup in _IG_IMPORT_SETUPS or setup.startswith("ig|") or src == "ig_import"


def deduplicate_ig_imports(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove IG-import rows that duplicate agent-placed or other import rows.

    Two rows are duplicates when they share direction, rounded GBP P&L, and close
    times within 10 minutes. Agent-placed rows are preferred; among pure IG-import
    pairs the row with a real market name is kept.
    """

    def parse_ts(row: dict[str, Any]) -> datetime | None:
        ts = row.get("closed_at")
        if not ts:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(str(ts)[:19], fmt)
            except ValueError:
                continue
        return None

    def pnl_key(row: dict[str, Any]) -> float | None:
        v = row.get("ig_pnl_currency")
        if v is None:
            return None
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    def within_window(a: dict[str, Any], b: dict[str, Any], window: timedelta) -> bool:
        ta, tb = parse_ts(a), parse_ts(b)
        if ta is None or tb is None:
            return True
        return abs(ta - tb) <= window

    window = timedelta(minutes=10)
    agent_rows = [r for r in rows if not is_ig_import_row(r)]
    import_rows = [r for r in rows if is_ig_import_row(r)]

    shadowed: set[int] = set()
    for idx, imp in enumerate(import_rows):
        imp_pnl = pnl_key(imp)
        imp_dir = str(imp.get("side") or "")
        if imp_pnl is None:
            continue
        for agent in agent_rows:
            if str(agent.get("side") or "") != imp_dir:
                continue
            if pnl_key(agent) != imp_pnl:
                continue
            if not within_window(agent, imp, window):
                continue
            shadowed.add(idx)
            break

    remaining_imports = [r for i, r in enumerate(import_rows) if i not in shadowed]

    kept_imports: list[dict[str, Any]] = []
    import_shadowed: set[int] = set()
    for i, row_a in enumerate(remaining_imports):
        if i in import_shadowed:
            continue
        for j, row_b in enumerate(remaining_imports):
            if j <= i or j in import_shadowed:
                continue
            if str(row_a.get("side") or "") != str(row_b.get("side") or ""):
                continue
            if pnl_key(row_a) != pnl_key(row_b):
                continue
            if not within_window(row_a, row_b, window):
                continue
            if row_b.get("market") and not row_a.get("market"):
                import_shadowed.add(i)
            else:
                import_shadowed.add(j)
        if i not in import_shadowed:
            kept_imports.append(row_a)

    return agent_rows + kept_imports
