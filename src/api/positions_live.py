"""Fast open-positions snapshot for GUI — IG sync cache only, no blocking REST."""

from __future__ import annotations

import json
import time
from typing import Any

# Prefer shared broker snapshot for this long — wrappers often poll every 10–30s.
# Older than this still merges into trade_support overlay / gbp_track enrichment.
_BROKER_SNAPSHOT_MAX_AGE_SEC = 60.0
_TRADE_SUPPORT_MAX_AGE_SEC = 90.0
_LAST_GOOD_PAYLOAD: dict[str, Any] | None = None
_LAST_GOOD_TS: float = 0.0


def remember_live_positions_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Cache last non-timeout payload for operator-safe timeout responses."""
    global _LAST_GOOD_PAYLOAD, _LAST_GOOD_TS
    if isinstance(payload, dict) and payload.get("error") != "timeout":
        _LAST_GOOD_PAYLOAD = dict(payload)
        _LAST_GOOD_TS = time.time()
    return payload


def last_good_live_positions_payload(
    *,
    error: str = "timeout",
) -> dict[str, Any]:
    """
    Return last-good book on handler timeout — never invent a flat empty book
    when we recently knew opens existed.
    """
    if _LAST_GOOD_PAYLOAD is None:
        return {
            "ok": False,
            "error": error,
            "count": 0,
            "positions": [],
            "unmonitored": 0,
            "verdict": "DEGRADED",
            "stale": True,
            "critical": False,
            "critical_alarms": [f"positions_live_{error}_no_last_good"],
            "broker_open_sot": {
                "count": 0,
                "source": "none",
                "note": "timeout with no prior snapshot",
            },
        }
    out = dict(_LAST_GOOD_PAYLOAD)
    out["ok"] = False
    out["error"] = error
    out["stale"] = True
    out["last_good_age_sec"] = round(max(0.0, time.time() - _LAST_GOOD_TS), 1)
    alarms = list(out.get("critical_alarms") or [])
    alarms.insert(0, f"positions_live_{error}_serving_last_good")
    out["critical_alarms"] = alarms[:12]
    if int(out.get("count") or 0) > 0 or int(
        (out.get("trade_support") or {}).get("broker_open") or 0
    ) > 0:
        out["critical"] = True
        out["verdict"] = "CRITICAL"
        out["protection_note"] = (
            f"CRITICAL: positions/live {error} — serving last-good open book. "
            "Do not treat as FLAT."
        )
    else:
        out["verdict"] = "DEGRADED"
    return out


def _sync_context(sync: Any | None) -> dict[str, Any]:
    if sync is None:
        return {
            "sync_age_sec": None,
            "sync_status": "missing",
            "stale": True,
            "rate_limit_paused": False,
        }
    try:
        snap = sync.snapshot()
        ts = float(getattr(sync, "_last_sync_ts", 0) or 0)
        sync_age = max(0.0, time.time() - ts) if ts > 0 else None
        status = str(getattr(snap, "sync_status", "") or "idle")
        paused = bool(getattr(snap, "rate_limit_paused", False))
        stale = not sync.is_fresh() if hasattr(sync, "is_fresh") else True
        return {
            "sync_age_sec": sync_age,
            "sync_status": status,
            "stale": stale or paused,
            "rate_limit_paused": paused,
        }
    except Exception:
        return {
            "sync_age_sec": None,
            "sync_status": "error",
            "stale": True,
            "rate_limit_paused": False,
        }


def _build_protection_summary(
    deal_id: str,
    *,
    gbp_tracks: dict[str, Any],
    stop_tracks: dict[str, Any],
    dyn_tracks: dict[str, Any],
    broker_stop_level: float | None = None,
    broker_limit_level: float | None = None,
) -> dict[str, Any]:
    """Operator-facing protection card — software stack vs broker-visible levels."""
    gbp = gbp_tracks.get(deal_id) or {}
    vrow = stop_tracks.get(deal_id) or {}
    dyn = dyn_tracks.get(deal_id) or {}
    gbp_armed = deal_id in gbp_tracks
    virtual_armed = deal_id in stop_tracks
    dynamic_armed = deal_id in dyn_tracks
    broker_stop = float(broker_stop_level) if broker_stop_level else None
    broker_limit = float(broker_limit_level) if broker_limit_level else None
    has_broker_stop = broker_stop is not None and broker_stop > 0
    has_broker_limit = broker_limit is not None and broker_limit > 0
    peak_gbp = gbp.get("peak_profit_gbp")
    trail_floor = gbp.get("trail_floor_gbp")
    loss_cap = gbp.get("loss_cap_gbp")
    soft_loss = gbp.get("soft_loss_gbp")
    target = gbp.get("target_profit_gbp")
    ceiling_pts = vrow.get("ceiling_pts")
    peak_ig = dyn.get("peak_profit_ig_pts")
    trail_trigger = gbp.get("trail_trigger_gbp")
    broker_sync = (
        "active"
        if dynamic_armed and peak_ig is not None and float(peak_ig or 0) > 0
        else "after_profit"
    )
    if gbp_armed and virtual_armed and dynamic_armed:
        mode = "software_primary"
    elif has_broker_stop or has_broker_limit:
        mode = "hybrid"
    else:
        mode = "broker_absent"
    layers_ok = gbp_armed and virtual_armed and dynamic_armed
    note = (
        "Protection runs in Trading Desk (GBP watchdog + virtual stop + dynamic trail). "
        "Blank IG limits are expected only while flatten REST works; "
        "failed flatten / EDITS_ONLY is a critical alarm."
    )
    if not layers_ok:
        note = "Incomplete risk stack — run position reconcile or OpenPositionManager tick."
    elif has_broker_stop and not has_broker_limit:
        note = (
            "Wide broker stop may appear on IG; profit limits are software-managed until trail syncs."
        )
    return {
        "mode": mode,
        "layers_armed": layers_ok,
        "gbp_armed": gbp_armed,
        "virtual_armed": virtual_armed,
        "dynamic_armed": dynamic_armed,
        "loss_cap_gbp": loss_cap,
        "soft_loss_gbp": soft_loss,
        "target_gbp": target,
        "trail_floor_gbp": trail_floor,
        "peak_profit_gbp": peak_gbp,
        "virtual_ceiling_pts": ceiling_pts,
        "broker_stop_level": broker_stop,
        "broker_limit_level": broker_limit,
        "broker_trail_sync": broker_sync,
        "operator_note": note,
    }


def _row_from_sync_position(
    p: Any,
    *,
    gbp_tracks: dict[str, Any],
    stop_tracks: dict[str, Any],
    dyn_tracks: dict[str, Any],
    stop_ids: set[str],
    dyn_ids: set[str],
    source: str = "sync_cache",
) -> dict[str, Any]:
    from execution.position_pnl_gbp import pnl_gbp_for_open_row

    deal_id = str(p.deal_id or "").strip()
    pnl = pnl_gbp_for_open_row(
        epic=p.epic,
        direction=p.direction,
        entry_level=float(p.level),
        size=float(p.size),
        upl=float(p.upl) if abs(float(p.upl)) >= 0.001 else None,
        bid=float(p.bid),
        offer=float(p.offer),
        currency=str(p.currency or ""),
    )
    gbp = gbp_tracks.get(deal_id) or {}
    broker_stop = float(getattr(p, "stop_level", 0) or 0) or None
    broker_limit = float(getattr(p, "limit_level", 0) or 0) or None
    row = {
        "deal_id": deal_id,
        "epic": p.epic,
        "direction": p.direction,
        "size": float(p.size),
        "entry": float(p.level),
        "pnl_gbp": round(float(pnl), 2) if pnl is not None else None,
        "gbp_armed": deal_id in gbp_tracks,
        "virtual_armed": deal_id in stop_ids,
        "dynamic_armed": deal_id in dyn_ids,
        "peak_profit_gbp": gbp.get("peak_profit_gbp"),
        "trail_floor_gbp": gbp.get("trail_floor_gbp"),
        "loss_cap_gbp": gbp.get("loss_cap_gbp"),
        "soft_loss_gbp": gbp.get("soft_loss_gbp"),
        "target_gbp": gbp.get("target_profit_gbp"),
        "broker_stop_level": broker_stop,
        "broker_limit_level": broker_limit,
        "source": source,
    }
    row["protection_summary"] = _build_protection_summary(
        deal_id,
        gbp_tracks=gbp_tracks,
        stop_tracks=stop_tracks,
        dyn_tracks=dyn_tracks,
        broker_stop_level=broker_stop,
        broker_limit_level=broker_limit,
    )
    return row


def _enrich_position_row(
    row: dict[str, Any],
    *,
    gbp_tracks: dict[str, Any],
    stop_tracks: dict[str, Any],
    dyn_tracks: dict[str, Any],
) -> dict[str, Any]:
    deal_id = str(row.get("deal_id") or "").strip()
    if not deal_id:
        return row
    broker_stop = row.get("broker_stop_level")
    broker_limit = row.get("broker_limit_level")
    row["protection_summary"] = _build_protection_summary(
        deal_id,
        gbp_tracks=gbp_tracks,
        stop_tracks=stop_tracks,
        dyn_tracks=dyn_tracks,
        broker_stop_level=broker_stop,
        broker_limit_level=broker_limit,
    )
    return row


def _read_trade_support_status(
    *,
    max_age_sec: float = _TRADE_SUPPORT_MAX_AGE_SEC,
) -> dict[str, Any] | None:
    """Broker-authoritative supervisor status file (no REST)."""
    try:
        from system.paths import data_dir

        path = data_dir() / "trade_support_status.json"
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        ts = float(raw.get("ts") or 0)
        age = max(0.0, time.time() - ts) if ts > 0 else None
        if age is not None and age > max_age_sec:
            return None
        raw["status_age_sec"] = round(age, 1) if age is not None else None
        raw["running"] = bool(age is not None and age < 60.0)
        # SoT honesty — never treat file broker_open=0 as truth when snapshot has opens.
        try:
            from runtime import broker_snapshot

            snap = broker_snapshot.read_snapshot(max_age_sec=None) or {}
            snap_n = int(snap.get("count") or len(snap.get("positions") or []))
            status_n = int(raw.get("broker_open") or 0)
            raw["snapshot_open"] = snap_n
            if status_n == 0 and snap_n > 0:
                raw["broker_open"] = snap_n
                raw["sot_overlay"] = True
        except Exception:
            pass
        return raw
    except Exception:
        return None


def _failed_flatten_actions(ts_status: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not ts_status:
        return []
    out: list[dict[str, Any]] = []
    for act in ts_status.get("actions") or []:
        if not isinstance(act, dict):
            continue
        if str(act.get("action") or "") != "flatten":
            continue
        if act.get("ok"):
            continue
        out.append(act)
    return out


def _pnl_by_deal_from_trade_support(
    ts_status: dict[str, Any] | None,
) -> dict[str, float]:
    if not ts_status:
        return {}
    by_deal: dict[str, float] = {}
    for act in ts_status.get("actions") or []:
        if not isinstance(act, dict):
            continue
        deal_id = str(act.get("deal_id") or "").strip()
        pnl = act.get("pnl_gbp")
        if not deal_id or pnl is None:
            continue
        try:
            by_deal[deal_id] = round(float(pnl), 2)
        except (TypeError, ValueError):
            continue
    return by_deal


def _apply_trade_support_overlay(
    rows: list[dict[str, Any]],
    ts_status: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Enrich rows with broker P&L / failed-flatten alarms from trade_support."""
    alarms: list[str] = []
    if not ts_status:
        return rows, alarms

    pnl_by_deal = _pnl_by_deal_from_trade_support(ts_status)
    failed = _failed_flatten_actions(ts_status)
    failed_by_deal = {
        str(a.get("deal_id") or "").strip(): a for a in failed if a.get("deal_id")
    }

    for row in rows:
        deal_id = str(row.get("deal_id") or "").strip()
        if row.get("pnl_gbp") is None and deal_id in pnl_by_deal:
            row["pnl_gbp"] = pnl_by_deal[deal_id]
            row["pnl_source"] = "trade_support"
        fail = failed_by_deal.get(deal_id)
        if fail:
            err = str(fail.get("error") or fail.get("reason") or "flatten_failed")
            row["flatten_failed"] = True
            row["flatten_error"] = err
            row["critical_alarm"] = True
            alarms.append(f"flatten_failed:{deal_id[:12]}:{err[:80]}")

    broker_open = int(ts_status.get("broker_open") or 0)
    known = {str(r.get("deal_id") or "") for r in rows}
    # Never synthesize hollow ghosts (entry==0 / pnl null) into the open array.
    # Missing broker deals raise alarms only — MemoryContext hard-vetoes ghosts.
    if broker_open > len(rows):
        for act in ts_status.get("actions") or []:
            if not isinstance(act, dict):
                continue
            deal_id = str(act.get("deal_id") or "").strip()
            if not deal_id or deal_id in known:
                continue
            entry_raw = act.get("entry") or act.get("entry_level")
            try:
                entry_f = float(entry_raw) if entry_raw is not None else 0.0
            except (TypeError, ValueError):
                entry_f = 0.0
            pnl = act.get("pnl_gbp")
            try:
                pnl_f = round(float(pnl), 2) if pnl is not None else None
            except (TypeError, ValueError):
                pnl_f = None
            if entry_f <= 0.0 or pnl_f is None:
                alarms.append(f"broker_open_unverified:{deal_id[:12]}")
                if not act.get("ok"):
                    alarms.append(
                        f"broker_open_missing_from_cache:{deal_id[:12]}"
                    )
                continue
            row = {
                "deal_id": deal_id,
                "epic": str(act.get("epic") or ""),
                "direction": str(act.get("direction") or "BUY"),
                "size": float(act.get("size") or 0.0),
                "entry": entry_f,
                "pnl_gbp": pnl_f,
                "gbp_armed": False,
                "virtual_armed": False,
                "dynamic_armed": False,
                "source": "trade_support_overlay",
                "pnl_source": "trade_support",
                "flatten_failed": not bool(act.get("ok")),
                "flatten_error": str(act.get("error") or "") or None,
                "critical_alarm": not bool(act.get("ok")),
            }
            rows.append(row)
            known.add(deal_id)
            if not act.get("ok"):
                alarms.append(
                    f"broker_open_missing_from_cache:{deal_id[:12]}"
                )

    if broker_open > 0 and int(ts_status.get("unvalued") or 0) > 0:
        alarms.append(f"trade_support_unvalued:{ts_status.get('unvalued')}")

    return rows, alarms


def build_live_positions_payload(
    *,
    allow_blocking_refresh: bool = False,
) -> dict[str, Any]:
    """
    Return open positions for Trading Desk UI.

    Default path is cache-only (sync snapshot + GBP tracks). Never blocks on REST
    unless ``allow_blocking_refresh=True`` (CLI/diagnostics only).
    """
    from runtime.dynamic_limit_engine import snapshot as dyn_snap
    from runtime.micro_gbp_exit import snapshot as gbp_snap
    from runtime.virtual_stop_loss import virtual_stop_snapshot

    gbp_tracks = gbp_snap().get("tracks") or {}
    stop_tracks = {
        str(p.get("deal_id") or p.get("track_id") or ""): p
        for p in (virtual_stop_snapshot().get("positions") or [])
        if str(p.get("deal_id") or p.get("track_id") or "")
    }
    dyn_tracks = dyn_snap().get("tracks") or {}
    stop_ids = set(stop_tracks.keys())
    dyn_ids = set(dyn_tracks.keys())

    rows: list[dict[str, Any]] = []
    sync_ctx = _sync_context(None)
    ts_status = _read_trade_support_status()

    # Fast path: shared broker snapshot (no locks, no REST, sub-ms).
    try:
        from runtime import broker_snapshot

        shared = broker_snapshot.read_snapshot(max_age_sec=_BROKER_SNAPSHOT_MAX_AGE_SEC)
        if shared and shared.get("positions"):
            for p in shared.get("positions") or []:
                deal_id = str(p.get("deal_id") or "").strip()
                if not deal_id:
                    continue
                rows.append(
                    _enrich_position_row(
                        {
                            "deal_id": deal_id,
                            "epic": str(p.get("epic") or ""),
                            "direction": str(p.get("direction") or "BUY"),
                            "size": float(p.get("size") or 0),
                            "entry": float(p.get("entry") or 0),
                            "pnl_gbp": p.get("pnl_gbp"),
                            "gbp_armed": deal_id in gbp_tracks,
                            "virtual_armed": deal_id in stop_ids,
                            "dynamic_armed": deal_id in dyn_ids,
                            "peak_profit_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                "peak_profit_gbp"
                            ),
                            "trail_floor_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                "trail_floor_gbp"
                            ),
                            "loss_cap_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                "loss_cap_gbp"
                            ),
                            "soft_loss_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                "soft_loss_gbp"
                            ),
                            "target_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                "target_profit_gbp"
                            ),
                            "broker_stop_level": p.get("stop_level") or p.get("stop"),
                            "broker_limit_level": p.get("limit_level") or p.get("limit"),
                            "source": f"broker_snapshot({shared.get('source')})",
                        },
                        gbp_tracks=gbp_tracks,
                        stop_tracks=stop_tracks,
                        dyn_tracks=dyn_tracks,
                    )
                )
            age = float(shared.get("age_sec") or 0)
            sync_ctx = {
                "sync_age_sec": age,
                "sync_status": f"broker_snapshot@{shared.get('age_sec')}s",
                "stale": age > 15.0,
                "rate_limit_paused": False,
            }
    except Exception:
        pass

    sync = None
    if not rows:
        try:
            from runtime.agent_bootstrap import get_ig_position_sync

            sync = get_ig_position_sync()
            sync_ctx = _sync_context(sync)
            if sync is not None:
                snap = sync.snapshot()
                for p in getattr(snap, "positions", []) or []:
                    deal_id = str(p.deal_id or "").strip()
                    if not deal_id:
                        continue
                    rows.append(
                        _row_from_sync_position(
                            p,
                            gbp_tracks=gbp_tracks,
                            stop_tracks=stop_tracks,
                            dyn_tracks=dyn_tracks,
                            stop_ids=stop_ids,
                            dyn_ids=dyn_ids,
                        )
                    )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "count": 0,
                "positions": [],
                "stale": True,
                **sync_ctx,
            }

    if not rows and allow_blocking_refresh and sync is not None:
        try:
            if hasattr(sync, "sync_once"):
                sync.sync_once()
                snap = sync.snapshot()
                sync_ctx = _sync_context(sync)
                for p in getattr(snap, "positions", []) or []:
                    deal_id = str(p.deal_id or "").strip()
                    if not deal_id:
                        continue
                    rows.append(
                        _row_from_sync_position(
                            p,
                            gbp_tracks=gbp_tracks,
                            stop_tracks=stop_tracks,
                            dyn_tracks=dyn_tracks,
                            stop_ids=stop_ids,
                            dyn_ids=dyn_ids,
                            source="sync_refresh",
                        )
                    )
        except Exception:
            pass

    # Cross-process truth when in-process sync is empty/stale (fallback only).
    if (not rows or sync_ctx.get("stale")) and not (
        rows and rows[0].get("source", "").startswith("broker_snapshot")
    ):
        try:
            from runtime import broker_snapshot

            shared = broker_snapshot.read_snapshot(
                max_age_sec=_BROKER_SNAPSHOT_MAX_AGE_SEC
            )
            if shared and (shared.get("positions") or (not rows)):
                snap_rows = []
                for p in shared.get("positions") or []:
                    deal_id = str(p.get("deal_id") or "").strip()
                    if not deal_id:
                        continue
                    snap_rows.append(
                        _enrich_position_row(
                            {
                                "deal_id": deal_id,
                                "epic": str(p.get("epic") or ""),
                                "direction": str(p.get("direction") or "BUY"),
                                "size": float(p.get("size") or 0),
                                "entry": float(p.get("entry") or 0),
                                "pnl_gbp": p.get("pnl_gbp"),
                                "gbp_armed": deal_id in gbp_tracks,
                                "virtual_armed": deal_id in stop_ids,
                                "dynamic_armed": deal_id in dyn_ids,
                                "peak_profit_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                    "peak_profit_gbp"
                                ),
                                "trail_floor_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                    "trail_floor_gbp"
                                ),
                                "loss_cap_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                    "loss_cap_gbp"
                                ),
                                "soft_loss_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                    "soft_loss_gbp"
                                ),
                                "target_gbp": (gbp_tracks.get(deal_id) or {}).get(
                                    "target_profit_gbp"
                                ),
                                "broker_stop_level": p.get("stop_level") or p.get("stop"),
                                "broker_limit_level": p.get("limit_level") or p.get("limit"),
                                "source": f"broker_snapshot({shared.get('source')})",
                            },
                            gbp_tracks=gbp_tracks,
                            stop_tracks=stop_tracks,
                            dyn_tracks=dyn_tracks,
                        )
                    )
                # Prefer the shared snapshot when it holds more positions than the
                # (possibly stale) in-process cache — broker truth wins.
                if len(snap_rows) >= len(rows):
                    rows = snap_rows
                    age = float(shared.get("age_sec") or 0)
                    sync_ctx["stale"] = age > 15.0
                    sync_ctx["sync_status"] = f"broker_snapshot@{shared.get('age_sec')}s"
                    sync_ctx["sync_age_sec"] = age
        except Exception:
            pass

    pnl_from_ts = _pnl_by_deal_from_trade_support(ts_status)
    if not rows and gbp_tracks:
        for deal_id, gbp in gbp_tracks.items():
            if not isinstance(gbp, dict):
                continue
            rows.append(
                _enrich_position_row(
                    {
                        "deal_id": deal_id,
                        "epic": str(gbp.get("epic") or ""),
                        "direction": str(gbp.get("direction") or "BUY"),
                        "size": float(gbp.get("size") or 0.5),
                        "entry": float(gbp.get("entry_level") or 0),
                        "pnl_gbp": pnl_from_ts.get(deal_id),
                        "gbp_armed": True,
                        "virtual_armed": deal_id in stop_ids,
                        "dynamic_armed": deal_id in dyn_ids,
                        "peak_profit_gbp": gbp.get("peak_profit_gbp"),
                        "trail_floor_gbp": gbp.get("trail_floor_gbp"),
                        "loss_cap_gbp": gbp.get("loss_cap_gbp"),
                        "soft_loss_gbp": gbp.get("soft_loss_gbp"),
                        "target_gbp": gbp.get("target_profit_gbp"),
                        "source": "gbp_track_fallback",
                        "pnl_source": "trade_support" if deal_id in pnl_from_ts else None,
                    },
                    gbp_tracks=gbp_tracks,
                    stop_tracks=stop_tracks,
                    dyn_tracks=dyn_tracks,
                )
            )
        sync_ctx["stale"] = True

    rows, critical_alarms = _apply_trade_support_overlay(rows, ts_status)

    # Re-enrich protection after overlay-synthesized rows.
    for i, row in enumerate(rows):
        if row.get("source") == "trade_support_overlay" or "protection_summary" not in row:
            rows[i] = _enrich_position_row(
                row,
                gbp_tracks=gbp_tracks,
                stop_tracks=stop_tracks,
                dyn_tracks=dyn_tracks,
            )

    # In-memory matrix: hard-veto hollow ghosts (entry==0 / pnl null).
    try:
        from system.memory_context import get_memory_context

        rows = get_memory_context().sync_open_rows(rows)
    except Exception:
        kept: list[dict[str, Any]] = []
        for r in rows:
            if r.get("critical_alarm") or r.get("flatten_failed"):
                kept.append(r)
                continue
            try:
                ent = float(r.get("entry") or 0)
            except (TypeError, ValueError):
                ent = 0.0
            if ent > 0 and r.get("pnl_gbp") is not None:
                kept.append(r)
        rows = kept

    unmonitored = sum(1 for r in rows if not r.get("gbp_armed"))
    layers_incomplete = sum(
        1
        for r in rows
        if not (
            r.get("gbp_armed") and r.get("virtual_armed") and r.get("dynamic_armed")
        )
    )
    total_pnl = sum(float(r["pnl_gbp"]) for r in rows if r.get("pnl_gbp") is not None)
    # Prefer trade_support total when it has a fresher valued book.
    if ts_status and ts_status.get("total_unrealized_gbp") is not None:
        try:
            ts_total = float(ts_status["total_unrealized_gbp"])
            ts_broker = int(ts_status.get("broker_open") or 0)
            if ts_broker >= len(rows) and int(ts_status.get("valued") or 0) > 0:
                total_pnl = ts_total
        except (TypeError, ValueError):
            pass

    stale = bool(sync_ctx.get("stale")) or (
        bool(rows)
        and any(
            r.get("source") in ("gbp_track_fallback", "trade_support_overlay")
            for r in rows
        )
    )
    has_critical = bool(critical_alarms) or any(r.get("critical_alarm") for r in rows)

    # Soft-filter sync-age stale when the broker book is empty. A flat SoT with
    # count=0 must not red-line desk_support / liveness on cache noise alone.
    try:
        _ts_open_pre = int((ts_status or {}).get("broker_open") or 0)
    except (TypeError, ValueError):
        _ts_open_pre = 0
    if not rows and _ts_open_pre == 0 and not has_critical:
        stale = False

    trade_support_block: dict[str, Any] | None = None
    ts_broker_open = 0
    if ts_status:
        failed = _failed_flatten_actions(ts_status)
        ts_broker_open = int(ts_status.get("broker_open") or 0)
        trade_support_block = {
            "running": bool(ts_status.get("running")),
            "broker_open": ts_broker_open,
            "valued": int(ts_status.get("valued") or 0),
            "total_unrealized_gbp": ts_status.get("total_unrealized_gbp"),
            "status_age_sec": ts_status.get("status_age_sec"),
            "actions_failed": len(failed),
            "last_flatten_error": (
                str(failed[0].get("error") or failed[0].get("reason") or "")
                if failed
                else None
            ),
        }

    # Operator SoT: prefer trade_support broker_open, else row count / snapshot.
    # Also honour last-good broker_snapshot when row cache is empty but opens exist.
    snap_open = 0
    try:
        from runtime import broker_snapshot as _broker_snapshot

        _snap = _broker_snapshot.read_snapshot(max_age_sec=None) or {}
        snap_open = int(_snap.get("count") or 0)
    except Exception:
        snap_open = 0
    sot_count = max(ts_broker_open, len(rows), snap_open)
    if ts_status and ts_broker_open >= len(rows) and ts_broker_open > 0:
        sot_source = "trade_support"
    elif rows:
        sot_source = "rows"
    elif snap_open > 0:
        sot_source = "broker_snapshot"
    else:
        sot_source = "flat"

    # Stale sync_cache rows with broker_absent protection must not inflate SoT
    # when trade_support + snapshot both say flat (kills entries via MISMATCH).
    phantom_cache_rows = False
    if (
        ts_status
        and bool(ts_status.get("running"))
        and ts_broker_open == 0
        and snap_open == 0
        and rows
    ):
        def _is_phantom_row(r: dict[str, Any]) -> bool:
            prot = r.get("protection_summary") if isinstance(r.get("protection_summary"), dict) else {}
            mode = str(prot.get("mode") or "").lower()
            src = str(r.get("source") or "").lower()
            return mode == "broker_absent" or src in ("sync_cache", "memory", "ledger")

        if all(_is_phantom_row(r) for r in rows if isinstance(r, dict)):
            phantom_cache_rows = True
            sot_count = 0
            sot_source = "trade_support"
            rows = []
            total_pnl = 0.0
            unmonitored = 0
            layers_incomplete = 0
            has_critical = False
            critical_alarms = [
                a
                for a in critical_alarms
                if "broker_open" not in str(a) and "unmonitored" not in str(a)
            ]

    broker_open_sot = {
        "count": sot_count,
        "source": sot_source,
        "trade_support_open": ts_broker_open,
        "rows_open": len(rows),
        "total_pnl_gbp": round(float(total_pnl), 2),
        "phantom_cache_cleared": phantom_cache_rows,
    }

    # Never report FLAT when SoT still shows broker opens (cache hollow / unvalued).
    if sot_count > 0 and not rows:
        verdict = "CRITICAL" if (has_critical or ts_broker_open > 0 or snap_open > 0) else "DEGRADED"
        has_critical = True
        if f"broker_open_sot_hollow:{sot_count}" not in critical_alarms:
            critical_alarms.append(f"broker_open_sot_hollow:{sot_count}")
    elif not rows:
        verdict = "FLAT"
    elif has_critical:
        verdict = "CRITICAL"
    elif unmonitored > 0 or stale or layers_incomplete > 0:
        verdict = "DEGRADED"
    else:
        verdict = "HEALTHY"

    protection_note = ""
    if has_critical or (sot_count > 0 and not rows):
        protection_note = (
            "CRITICAL: flatten failed or broker open diverges from Desk cache. "
            "Do not trust FLAT/HEALTHY until trade_support broker_open=0."
        )
    elif rows:
        protection_note = (
            "Stops/limits are software-managed in Trading Desk; IG may show blank limits "
            "only while flatten REST works."
        )

    memory_snap: dict[str, Any] | None = None
    try:
        from system.memory_context import get_memory_context

        memory_snap = get_memory_context().snapshot()
    except Exception:
        memory_snap = None

    payload = {
        "ok": not has_critical,
        "count": len(rows),
        "total_pnl_gbp": round(float(total_pnl), 2),
        "unmonitored": unmonitored,
        "layers_incomplete": layers_incomplete,
        "verdict": verdict,
        "critical": has_critical,
        "critical_alarms": critical_alarms[:12],
        "protection_mode": "software_primary" if (rows or sot_count > 0) else "flat",
        "protection_note": protection_note,
        "trade_support": trade_support_block,
        "broker_open_sot": broker_open_sot,
        "memory_context": memory_snap,
        "positions": rows,
        **sync_ctx,
        # Authoritative after sync_ctx — flat SoT must win over cache-age noise.
        "stale": stale,
    }
    return remember_live_positions_payload(payload)
