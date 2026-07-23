"""
Shared broker-truth snapshot — single source of truth for open positions.

The agent (``IgPositionSync``) and the out-of-process supervisors
(``trade_support_wrapper``, ``manage_live_positions``) each used to poll IG
``/positions`` independently. That produced (a) divergent stale caches — the GUI
once showed 3 positions @ 0.5 while the broker held 5 @ 1.5 — and (b) combined
REST traffic well over IG's 3-calls/min budget.

This module gives every process ONE authoritative, freshest-wins snapshot on
disk. Any process that successfully reads the broker writes it here; any process
that needs the open book reads here first and only hits REST when the snapshot is
stale. That collapses redundant polling (helping the shared REST budget) and
eliminates cache divergence.

The snapshot is intentionally tiny and atomic (temp + ``os.replace``) so readers
never see a half-written file, and it is safe to read/write from multiple
processes without a lock (last writer wins; readers tolerate a slightly older
snapshot via ``max_age_sec``).

Path note: primary path follows ``data_dir()`` at call time. Readers also
consider the legacy ``src/data/state/`` mirror and pick the freshest file so a
process that imported with a frozen/legacy root cannot age out of a repaired
v31 snapshot (and vice versa).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from system.paths import data_dir, legacy_src_data_dir, state_dir

# Default freshness window a reader will trust before it polls REST itself.
DEFAULT_MAX_AGE_SEC = 8.0


def snapshot_path() -> Path:
    """Primary snapshot path for this process (resolved live — not import-frozen)."""
    return state_dir() / "broker_snapshot.json"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _write_paths() -> list[Path]:
    """Writable mirrors — primary + legacy only (never a hardcoded prod path).

    Hardcoding ``v31-production/...`` on write let pytest tmp-root cases stamp
    the live desk snapshot; reads may still consult that bridge path.
    """
    return _dedupe_paths(
        [
            snapshot_path(),
            legacy_src_data_dir() / "state" / "broker_snapshot.json",
        ]
    )


def _mirror_paths() -> list[Path]:
    """Candidate snapshot locations — primary first, then legacy + prod bridge."""
    return _dedupe_paths(
        [
            snapshot_path(),
            legacy_src_data_dir() / "state" / "broker_snapshot.json",
            Path(__file__).resolve().parents[1]
            / "data"
            / "v31-production"
            / "state"
            / "broker_snapshot.json",
        ]
    )


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def lookup_mark_quote(
    epic: str,
    *,
    entry: float | None = None,
) -> dict[str, Any] | None:
    """Resolve bid/offer for snapshot enrichment (hub → ring → fulfillment cache).

    Scale-checked against ``entry`` when provided so Yahoo/index mismatches never
    poison GBP valuation. Safe for out-of-process supervisors (disk cache path).
    """
    key = str(epic or "").strip()
    if not key:
        return None

    candidates: list[dict[str, Any]] = []

    # 1) In-process market data hub (agent / OPM).
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(key)
        if snap is not None:
            bid = float(getattr(snap, "bid", 0) or 0)
            offer = float(getattr(snap, "offer", 0) or 0)
            if bid > 0 and offer > 0:
                candidates.append(
                    {
                        "bid": bid,
                        "offer": offer,
                        "mid": (bid + offer) / 2.0,
                        "source": str(getattr(snap, "source", "") or "market_data_hub"),
                    }
                )
    except Exception:
        pass

    # 2) In-process alpha quote ring (multi_feed race wins).
    try:
        from system.ipc.ring_buffer import get_alpha_ring_buffer

        row = get_alpha_ring_buffer().read_quote_for_epic(key)
        if row is not None:
            bid, offer, _seq = row
            if bid > 0 and offer > 0:
                candidates.append(
                    {
                        "bid": float(bid),
                        "offer": float(offer),
                        "mid": (float(bid) + float(offer)) / 2.0,
                        "source": "alpha_ring",
                    }
                )
    except Exception:
        pass

    # 3) Disk fulfillment cache — trade_support out-of-process SoT for marks.
    try:
        path = data_dir() / "state" / "fulfillment_cache.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            quotes = payload.get("market_quotes") or {}
            q = quotes.get(key) if isinstance(quotes, dict) else None
            if isinstance(q, dict):
                bid = _safe_float(q.get("bid")) or 0.0
                offer = _safe_float(q.get("offer")) or 0.0
                if bid > 0 and offer > 0:
                    mid = _safe_float(q.get("mid"))
                    if mid is None or mid <= 0:
                        mid = (bid + offer) / 2.0
                    candidates.append(
                        {
                            "bid": bid,
                            "offer": offer,
                            "mid": float(mid),
                            "source": f"fulfillment_cache({q.get('source') or 'disk'})",
                        }
                    )
    except Exception:
        pass

    if not candidates:
        return None

    entry_f = _safe_float(entry) if entry is not None else None
    if entry_f is not None and entry_f > 0:
        try:
            from trading.open_position_view import _quote_mark_trustworthy

            for cand in candidates:
                mark = float(cand["mid"] or 0) or (
                    (float(cand["bid"]) + float(cand["offer"])) / 2.0
                )
                if _quote_mark_trustworthy(entry_f, mark, key):
                    return cand
        except Exception:
            pass
        return None
    return candidates[0]


def enrich_snapshot_positions(
    positions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Fill missing bid/offer/mid + recompute pnl_gbp when coalesced REST omitted marks."""
    from execution.position_pnl_gbp import pnl_gbp_for_open_row

    out: list[dict[str, Any]] = []
    for raw in positions or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        epic = str(row.get("epic") or "").strip()
        bid = _safe_float(row.get("bid")) or 0.0
        offer = _safe_float(row.get("offer")) or 0.0
        entry = _safe_float(row.get("entry")) or 0.0
        if (bid <= 0 or offer <= 0) and epic:
            quote = lookup_mark_quote(epic, entry=entry if entry > 0 else None)
            if quote:
                bid = float(quote["bid"])
                offer = float(quote["offer"])
                row["bid"] = bid
                row["offer"] = offer
                row["mid"] = float(quote.get("mid") or ((bid + offer) / 2.0))
                row["quote_source"] = str(quote.get("source") or "enriched")
        elif bid > 0 and offer > 0:
            mid = _safe_float(row.get("mid"))
            if mid is None or mid <= 0:
                row["mid"] = (bid + offer) / 2.0

        if row.get("pnl_gbp") is None and bid > 0 and offer > 0 and entry > 0:
            try:
                size = float(row.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            pnl = pnl_gbp_for_open_row(
                epic=epic,
                direction=str(row.get("direction") or "BUY").upper(),
                entry_level=entry,
                size=size,
                bid=bid,
                offer=offer,
            )
            if pnl is not None:
                row["pnl_gbp"] = round(float(pnl), 2)
                row.setdefault("pnl_source", "enriched_quote")
        out.append(row)
    return out


def enrich_ig_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Inject bid/offer into hollow IG-shaped items before valuation."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        cloned = dict(item)
        pos = dict(cloned.get("position") or {})
        mkt = dict(cloned.get("market") or {})
        epic = str(mkt.get("epic") or pos.get("epic") or "").strip()
        bid = _safe_float(mkt.get("bid")) or 0.0
        offer = _safe_float(mkt.get("offer")) or 0.0
        entry = _safe_float(pos.get("level") or pos.get("openLevel")) or 0.0
        if (bid <= 0 or offer <= 0) and epic:
            quote = lookup_mark_quote(epic, entry=entry if entry > 0 else None)
            if quote:
                bid = float(quote["bid"])
                offer = float(quote["offer"])
                mkt["bid"] = bid
                mkt["offer"] = offer
                mkt["epic"] = epic
                mkt["_quote_source"] = str(quote.get("source") or "enriched")
                cloned["_enriched_quote"] = True
        if epic and not mkt.get("epic"):
            mkt["epic"] = epic
        cloned["position"] = pos
        cloned["market"] = mkt
        out.append(cloned)
    return out


def _normalize_from_ig_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from execution.position_pnl_gbp import pnl_gbp_from_ig_item

    enriched_items = enrich_ig_items(items)
    rows: list[dict[str, Any]] = []
    for item in enriched_items:
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
        epic = str(mkt.get("epic") or pos.get("epic") or "").strip()
        if not deal_id or not epic:
            continue
        try:
            size = float(pos.get("size") or 0)
            entry = float(pos.get("level") or 0)
        except (TypeError, ValueError):
            continue
        pnl = pnl_gbp_from_ig_item(item)
        try:
            stop_level = float(pos.get("stopLevel") or pos.get("stop_level") or 0) or None
        except (TypeError, ValueError):
            stop_level = None
        try:
            limit_level = float(pos.get("limitLevel") or pos.get("limit_level") or 0) or None
        except (TypeError, ValueError):
            limit_level = None
        bid = _safe_float(mkt.get("bid"))
        offer = _safe_float(mkt.get("offer"))
        mid = None
        if bid and offer and bid > 0 and offer > 0:
            mid = (bid + offer) / 2.0
        row: dict[str, Any] = {
            "deal_id": deal_id,
            "epic": epic,
            "direction": str(pos.get("direction") or "BUY").upper(),
            "size": size,
            "entry": entry,
            "pnl_gbp": round(float(pnl), 2) if pnl is not None else None,
            "stop_level": stop_level,
            "limit_level": limit_level,
            "created": str(
                pos.get("createdDateUTC")
                or pos.get("createdDate")
                or pos.get("created")
                or ""
            ),
        }
        # Preserve broker deal currency — USDJPY opens are often JPY; FTSE GBP.
        # Net-close with the wrong currencyCode returns REJECTED/UNKNOWN.
        ccy = str(pos.get("currency") or pos.get("currencyCode") or "").upper().strip()
        if ccy:
            row["currency"] = ccy
        if bid and bid > 0:
            row["bid"] = bid
        if offer and offer > 0:
            row["offer"] = offer
        if mid and mid > 0:
            row["mid"] = mid
        if mkt.get("_quote_source"):
            row["quote_source"] = mkt.get("_quote_source")
        rows.append(row)
    return enrich_snapshot_positions(rows)


def _atomic_write(path: Path, payload: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError):
        return False


def write_snapshot(
    *,
    source: str,
    items: list[dict[str, Any]] | None = None,
    positions: list[dict[str, Any]] | None = None,
    account_upl: float | None = None,
) -> bool:
    """Atomically persist the broker open book (primary + legacy mirror).

    Provide either raw IG ``items`` (from ``rest.open_positions()``) or an
    already-normalized ``positions`` list. Never raises — snapshot writes must
    not break the caller's hot path.
    """
    try:
        if items is not None:
            rows = _normalize_from_ig_items(items)
        else:
            rows = enrich_snapshot_positions(list(positions or []))
        payload = {
            "ts": time.time(),
            "source": source,
            "pid": os.getpid(),
            "count": len(rows),
            "account_upl": account_upl,
            "positions": rows,
        }
        ok = False
        for path in _write_paths():
            if _atomic_write(path, payload):
                ok = True
        return ok
    except (OSError, ValueError, TypeError):
        return False


def _load_raw(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    age = time.time() - float(data.get("ts") or 0)
    data["age_sec"] = round(age, 2)
    data["_path"] = str(path)
    return data


def read_snapshot(max_age_sec: float | None = None) -> dict[str, Any] | None:
    """Return the freshest shared snapshot across primary + legacy mirrors.

    ``max_age_sec=None`` returns the snapshot regardless of age (with ``age_sec``
    populated so the caller can decide). A numeric value returns None when the
    freshest snapshot is older than the window.
    """
    best: dict[str, Any] | None = None
    best_ts = -1.0
    for path in _mirror_paths():
        data = _load_raw(path)
        if data is None:
            continue
        ts = float(data.get("ts") or 0)
        if ts >= best_ts:
            best_ts = ts
            best = data
    if best is None:
        return None
    if max_age_sec is not None and float(best.get("age_sec") or 0) > float(max_age_sec):
        return None
    # Permanent: hollow coalesce snapshots omit bid/offer — enrich on read so
    # trade_support / OPM never CRITICAL-unvalued solely from missing REST marks.
    try:
        positions = best.get("positions")
        if isinstance(positions, list) and positions:
            enriched = enrich_snapshot_positions(positions)
            healed = any(
                (
                    (e.get("bid") and not p.get("bid"))
                    or (e.get("offer") and not p.get("offer"))
                    or (e.get("pnl_gbp") is not None and p.get("pnl_gbp") is None)
                )
                for e, p in zip(enriched, positions)
                if isinstance(e, dict) and isinstance(p, dict)
            )
            if healed or enriched:
                best = dict(best)
                best["positions"] = enriched
                if healed:
                    best["quotes_enriched"] = True
    except Exception:
        pass
    return best


def snapshot_age_sec() -> float | None:
    snap = read_snapshot(max_age_sec=None)
    return snap.get("age_sec") if snap else None


def is_fresh(max_age_sec: float = DEFAULT_MAX_AGE_SEC) -> bool:
    return read_snapshot(max_age_sec=max_age_sec) is not None


def ig_items_from_snapshot(
    snap: dict[str, Any] | None = None,
    *,
    max_age_sec: float | None = 120.0,
) -> list[dict[str, Any]]:
    """Rebuild IG-shaped open-position items from last-good shared snapshot.

    Used under ``positions_coalesce_pressure`` so callers never hard-require a
    live GET /positions. Does not refresh the snapshot timestamp.
    """
    if snap is None:
        snap = read_snapshot(max_age_sec=max_age_sec)
    if not snap:
        return []
    items: list[dict[str, Any]] = []
    for p in snap.get("positions") or []:
        if not isinstance(p, dict):
            continue
        deal_id = str(p.get("deal_id") or "").strip()
        epic = str(p.get("epic") or "").strip()
        if not deal_id or not epic:
            continue
        try:
            size = float(p.get("size") or 0)
            entry = float(p.get("entry") or 0)
        except (TypeError, ValueError):
            continue
        market: dict[str, Any] = {"epic": epic}
        bid = _safe_float(p.get("bid"))
        offer = _safe_float(p.get("offer"))
        if bid and bid > 0:
            market["bid"] = bid
        if offer and offer > 0:
            market["offer"] = offer
        if p.get("quote_source"):
            market["_quote_source"] = p.get("quote_source")
        items.append(
            {
                "position": {
                    "dealId": deal_id,
                    "dealID": deal_id,
                    "epic": epic,
                    "direction": str(p.get("direction") or "BUY").upper(),
                    "size": size,
                    "level": entry,
                    "openLevel": entry,
                    "stopLevel": p.get("stop_level") or 0.0,
                    "limitLevel": p.get("limit_level") or 0.0,
                    "createdDateUTC": str(p.get("created") or ""),
                    "createdDate": str(p.get("created") or ""),
                },
                "market": market,
                "_pnl_gbp": p.get("pnl_gbp"),
                "_from_snapshot": True,
            }
        )
    return enrich_ig_items(items)


def remove_deals_from_snapshot(
    deal_ids: list[str] | set[str],
    *,
    source: str = "local_close_patch",
) -> int:
    """Drop closed dealIds from shared snapshot without a REST re-poll."""
    want = {str(d).strip() for d in deal_ids if str(d).strip()}
    if not want:
        return 0
    snap = read_snapshot(max_age_sec=None)
    if not snap:
        return 0
    before = list(snap.get("positions") or [])
    after = [p for p in before if str(p.get("deal_id") or "").strip() not in want]
    removed = len(before) - len(after)
    if removed <= 0:
        return 0
    write_snapshot(
        source=source,
        positions=after,
        account_upl=snap.get("account_upl"),
    )
    return removed


def open_count_from_snapshot(*, max_age_sec: float | None = 300.0) -> int | None:
    """Return last-known broker open count, or None if no usable snapshot."""
    snap = read_snapshot(max_age_sec=max_age_sec)
    if snap is None:
        return None
    try:
        return int(snap.get("count") if snap.get("count") is not None else len(snap.get("positions") or []))
    except (TypeError, ValueError):
        return len(snap.get("positions") or [])


def verify_for_boot_hydrate(
    snap: dict[str, Any] | None = None,
    *,
    max_age_sec: float | None = None,
) -> dict[str, Any]:
    """
    Boot-gate verification — never invent opens from empty stubs.

    Delegates to ``runtime.boot_sot_fallback.verify_broker_snapshot_for_boot``.
    """
    from runtime.boot_sot_fallback import (
        read_verified_boot_snapshot,
        verify_broker_snapshot_for_boot,
    )

    if snap is not None:
        return verify_broker_snapshot_for_boot(snap)
    return read_verified_boot_snapshot(max_age_sec=max_age_sec)


def force_snapshot_sync(*, source: str = "force_snapshot_sync") -> dict[str, Any]:
    """Hot-path memory+disk sync: refresh snapshot mirrors and re-arm GBP tracks.

    Safe to call from an admin route inside the agent PID. Overwrites in-memory
    ``micro_gbp_exit`` tracks with entry levels from the freshest disk snapshot
    (or repair sidecar) so ``/api/positions/live`` stops serving
    ``gbp_track_fallback`` with ``entry=0``.
    """
    actions: list[str] = []
    snap = read_snapshot(max_age_sec=None) or {}
    positions = list(snap.get("positions") or [])

    # Prefer explicit repair sidecar entry when snapshot row still has entry<=0.
    try:
        repair_path = data_dir() / "state" / "stale_position_repair.json"
        if repair_path.is_file():
            repair = json.loads(repair_path.read_text(encoding="utf-8"))
            deal_id = str(repair.get("deal_id") or "").strip()
            entry = float(repair.get("entry") or 0)
            if deal_id and entry > 0:
                matched = False
                for row in positions:
                    if str(row.get("deal_id") or "") == deal_id:
                        if float(row.get("entry") or 0) <= 0:
                            row["entry"] = entry
                            if repair.get("pnl_gbp") is not None:
                                row["pnl_gbp"] = repair.get("pnl_gbp")
                            actions.append(f"repair_sidecar_merged:{deal_id[:12]}")
                        matched = True
                        break
                if not matched:
                    positions.append(
                        {
                            "deal_id": deal_id,
                            "epic": repair.get("epic"),
                            "direction": repair.get("direction") or "BUY",
                            "size": float(repair.get("size") or 0.5),
                            "entry": entry,
                            "pnl_gbp": repair.get("pnl_gbp"),
                        }
                    )
                    actions.append(f"repair_sidecar_row:{deal_id[:12]}")
    except (OSError, ValueError, TypeError) as exc:
        actions.append(f"repair_sidecar_skip:{type(exc).__name__}")

    rearmed = 0
    if not positions:
        actions.append("flat_book")
        cleanup: dict[str, Any] = {}
        try:
            from runtime.snapshot_mirror_cleanup import (
                maybe_deactivate_legacy_snapshot_mirror,
            )

            cleanup = maybe_deactivate_legacy_snapshot_mirror(open_count=0)
            actions.append("mirror_cleanup")
        except Exception as exc:
            actions.append(f"mirror_cleanup_skip:{type(exc).__name__}")
        return {
            "ok": True,
            "flat": True,
            "actions": actions,
            "count": 0,
            "rearmed": 0,
            "cleanup": cleanup,
        }

    wrote = write_snapshot(source=source, positions=positions, account_upl=snap.get("account_upl"))
    actions.append("broker_snapshot_mirrored" if wrote else "broker_snapshot_write_failed")

    try:
        from runtime.micro_gbp_exit import register_gbp_exit, snapshot as gbp_snap

        tracks = (gbp_snap().get("tracks") or {}) if callable(gbp_snap) else {}
        for row in positions:
            deal_id = str(row.get("deal_id") or "").strip()
            entry = float(row.get("entry") or 0)
            if not deal_id or entry <= 0:
                continue
            prev = tracks.get(deal_id) or {}
            register_gbp_exit(
                deal_id=deal_id,
                epic=str(row.get("epic") or prev.get("epic") or ""),
                direction=str(row.get("direction") or prev.get("direction") or "BUY"),
                size=float(row.get("size") or prev.get("size") or 0.5),
                entry_level=entry,
                loss_cap_gbp=float(prev.get("loss_cap_gbp") or 4.0),
                target_profit_gbp=float(prev.get("target_profit_gbp") or 8.0),
                trail_trigger_gbp=float(prev.get("trail_trigger_gbp") or 2.0),
                soft_loss_gbp=float(prev.get("soft_loss_gbp") or 2.2),
            )
            rearmed += 1
        actions.append(f"gbp_tracks_rearmed:{rearmed}")
    except Exception as exc:
        actions.append(f"gbp_track_skip:{type(exc).__name__}:{exc}")

    fresh = read_snapshot(max_age_sec=None) or {}
    return {
        "ok": True,
        "actions": actions,
        "count": len(positions),
        "rearmed": rearmed,
        "age_sec": fresh.get("age_sec"),
        "source": fresh.get("source"),
        "positions": [
            {
                "deal_id": p.get("deal_id"),
                "entry": p.get("entry"),
                "pnl_gbp": p.get("pnl_gbp"),
                "epic": p.get("epic"),
            }
            for p in positions
        ],
    }
