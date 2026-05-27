"""Merge IG transaction rows with local position-sync closes for UI display."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from system.engine_log import log_engine
from system.ig_transactions import _raw_has_clock_time, ig_deal_key_variants

PENDING_IG_CONFIRM_SEC = 2 * 3600
PENDING_RESULT_LABEL = "⏳ Pending IG confirm"
UNCONFIRMED_RESULT_LABEL = "Unconfirmed"
_unconfirmed_warned: set[str] = set()


def _all_deal_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("ig_deal_id", "deal_reference", "ig_close_deal_id"):
        val = str(row.get(field) or "").strip()
        if val:
            keys.update(ig_deal_key_variants(val))
    return keys


def _row_key(row: dict[str, Any]) -> str:
    ref = str(row.get("ig_deal_id") or row.get("deal_reference") or "").strip()
    if ref:
        return ref.upper()
    closed = str(row.get("closed_at") or "")
    side = str(row.get("side") or "")
    entry = float(row.get("entry") or 0)
    return f"{closed}|{side}|{entry:.2f}"


def _normalise_local_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    notes = str(out.get("notes") or "")
    if "IG sync" in notes or out.get("ig_deal_id"):
        out["source"] = out.get("source") or "ig_sync"
    else:
        out["source"] = out.get("source") or "local"
    return out


def _parse_closed_ts(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            part = text[:19] if " " in fmt else text[:10]
            return datetime.strptime(part, fmt)
        except ValueError:
            continue
    return None


def _is_ig_confirmed(row: dict[str, Any]) -> bool:
    if row.get("ig_pnl_currency") is not None:
        return True
    return str(row.get("source") or "") == "ig"


def _apply_pending_state(row: dict[str, Any], ig_index: set[str]) -> dict[str, Any]:
    """Mark local-only broker closes pending until IG transaction history confirms P&L."""
    out = dict(row)
    if _is_ig_confirmed(out):
        out["pending_ig_confirm"] = False
        out["unconfirmed_ig"] = False
        out.pop("display_result", None)
        return out

    keys = _all_deal_keys(out)
    has_broker_id = bool(keys) or bool(str(out.get("ig_deal_id") or "").strip())
    in_ig_cache = bool(ig_index and keys & ig_index)
    if not has_broker_id or in_ig_cache:
        out["pending_ig_confirm"] = False
        out["unconfirmed_ig"] = False
        return out

    out["pending_ig_confirm"] = True
    closed_dt = _parse_closed_ts(str(out.get("closed_at") or ""))
    age_sec = (datetime.now() - closed_dt).total_seconds() if closed_dt else 0.0
    if age_sec >= PENDING_IG_CONFIRM_SEC:
        out["unconfirmed_ig"] = True
        out["display_result"] = UNCONFIRMED_RESULT_LABEL
        warn_key = str(out.get("ig_deal_id") or out.get("deal_reference") or _row_key(out))
        if warn_key not in _unconfirmed_warned:
            _unconfirmed_warned.add(warn_key)
            log_engine(
                "Closed trade unconfirmed after 2h — no IG transaction match: "
                f"deal={warn_key[:16]} closed={out.get('closed_at', '')}"
            )
    else:
        out["unconfirmed_ig"] = False
        out["display_result"] = PENDING_RESULT_LABEL
    return out


def _ig_row_wins(existing: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Keep IG fields; only borrow a better timestamp from sync when IG is date-only."""
    local_ts = str(local.get("closed_at") or "")
    ig_ts = str(existing.get("closed_at") or "")
    if _raw_has_clock_time(local_ts) and not _raw_has_clock_time(ig_ts):
        return {**existing, "closed_at": local_ts}
    if _raw_has_clock_time(ig_ts) and not _raw_has_clock_time(local_ts):
        return {**existing, "closed_at": ig_ts}
    return existing


def merge_closed_trades(
    ig_rows: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
    *,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], str]:
    """
    IG transaction history is the source of truth when available.

    Local position-sync rows are only used to fill deals not yet present in IG
    history (e.g. a close in the last few seconds before the history API updates).
    """
    ig_index: set[str] = set()
    for row in ig_rows:
        ig_index.update(_all_deal_keys(row))

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for row in ig_rows:
        key = _row_key(row)
        if key not in merged:
            order.append(key)
        merged[key] = {**row, "source": "ig"}

    for row in local_rows:
        norm = _normalise_local_row(row)
        if ig_index and _all_deal_keys(norm) & ig_index:
            key = _row_key(norm)
            if key in merged:
                merged[key] = _ig_row_wins(merged[key], norm)
            continue
        key = _row_key(norm)
        if key in merged:
            merged[key] = _ig_row_wins(merged[key], norm)
            continue
        order.append(key)
        merged[key] = norm

    out = [_apply_pending_state(merged[k], ig_index) for k in order if k in merged]
    out.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
    out = out[:limit]

    if ig_rows and local_rows:
        label = "IG History + sync"
    elif ig_rows:
        label = "IG History"
    elif any(r.get("source") == "ig_sync" for r in out):
        label = "IG position sync"
    else:
        label = "local store"
    return out, label


def recent_deal_ids(rows: list[dict[str, Any]], *, limit: int = 8) -> list[str]:
    ids: list[str] = []
    for r in rows:
        ref = str(r.get("ig_deal_id") or r.get("deal_reference") or "").strip()
        if ref and ref not in ids:
            ids.append(ref)
        if len(ids) >= limit:
            break
    return ids
