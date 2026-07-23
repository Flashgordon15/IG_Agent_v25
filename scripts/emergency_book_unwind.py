#!/usr/bin/env python3
"""Emergency spaced net-close unwind — OPEN-side invert once, confirm FULLY_CLOSED.

Does NOT use close_position(verify=True) (that triggers flatten_epic on residual book).
Keeps entry_halt / trading_paused / deploy_hold ON. Never resumes entries.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path("/Users/chrisgordon/Projects/IG_Agent_v25")
DATA = ROOT / "src/data/v31-production"
STATE = DATA / "state"
LOG = DATA / "logs" / "emergency_book_unwind.log"
PROGRESS = STATE / "emergency_unwind_progress.json"

# Cascade mode: EMERGENCY_UNWIND_GAP_SEC=6 for large books; default gentle 18s.
GAP_SEC = float(os.environ.get("EMERGENCY_UNWIND_GAP_SEC", "18") or 18)
GAP_SEC = max(3.0, min(60.0, GAP_SEC))
TARGET_CAP = 6
TARGET_IDEAL = 0
# Raw GET /positions soft-capped ~1/min — sync sparsely during cascade.
SYNC_EVERY = int(os.environ.get("EMERGENCY_UNWIND_SYNC_EVERY", "20") or 20)
SYNC_EVERY = max(5, min(60, SYNC_EVERY))
# Rotate these reasons to back-of-book so one stuck epic cannot stall the unwind.
_ROTATE_REASONS = (
    "INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY",
    "MARKET_CLOSED",
    "MARKET_CLOSED_WITH_EDITS",
    "MARKET_NOT_OPEN",
    "INSTRUMENT_NOT_AVAILABLE",
    "ATTACHED_ORDER_LEVEL_ERROR",
    "UNKNOWN",
    "REJECTED",
)
_GOLD_MARKERS = ("GOLD", "XAU", "CFPGOLD")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def reinforce_pauses() -> None:
    ts = time.time()
    for name in ("entry_halt", "trading_paused", "deploy_hold"):
        p = STATE / f"{name}.json"
        p.write_text(
            json.dumps(
                {
                    "active": True,
                    "reason": "emergency_book_unwind_hold",
                    "ts": ts,
                    "operator": "emergency_book_unwind",
                },
                indent=2,
            )
        )


def write_progress(**kwargs) -> None:
    cur = {}
    if PROGRESS.exists():
        try:
            cur = json.loads(PROGRESS.read_text())
        except Exception:
            cur = {}
    cur.update(kwargs)
    cur["updated_at"] = time.time()
    PROGRESS.write_text(json.dumps(cur, indent=2, default=str))


def raw_positions(rest):
    """Forced broker ledger read — bypass soft positions-poll deferral for unwind."""
    try:
        from system.rest_api_budget import mark_post_boot_rest_guard

        # Do not tighten further during emergency; clear youth soft-cap pressure.
        mark_post_boot_rest_guard(grace_sec=0.1)
    except Exception:
        pass
    # Prefer adapter count path when available (same GET, clearer errors).
    try:
        if hasattr(rest, "open_positions"):
            # Force underlying request with priority so shared soft-cap cannot stall.
            r = rest.request(
                "GET",
                "/positions",
                headers=rest._auth_headers("2"),
                budget_priority=True,
            )
            if r.status_code == 200:
                return list((r.json() or {}).get("positions") or []), 200
            return None, r.status_code
    except Exception as exc:
        log(f"raw_positions adapter err {type(exc).__name__}: {exc}")
    try:
        r = rest.request(
            "GET",
            "/positions",
            headers=rest._auth_headers("2"),
            budget_priority=True,
        )
        if r.status_code != 200:
            return None, r.status_code
        return list((r.json() or {}).get("positions") or []), 200
    except Exception as exc:
        log(f"raw_positions request err {type(exc).__name__}: {exc}")
        return None, 0


def _epic_priority(epic: str) -> int:
    """Lower = close first. Prefer liquid index/FX; defer closed-session metals."""
    u = str(epic or "").upper()
    if any(m in u for m in _GOLD_MARKERS):
        return 90
    if "NIKKEI" in u or "JAPAN" in u:
        return 40
    if "FTSE" in u or "DAX" in u:
        return 30
    if "EURUSD" in u or "GBPUSD" in u or "USDJPY" in u or "AUDUSD" in u:
        return 20
    if "DOW" in u or "WALL" in u:
        return 10
    return 50


def _currency_candidates(
    epic: str, default_ccy: str, *, position_ccy: str | None = None
) -> list[str]:
    """Prefer the position's own currency (USDJPY opens are often JPY, FTSE GBP)."""
    u = str(epic or "").upper()
    default = str(default_ccy or "USD").upper()
    pos = str(position_ccy or "").upper().strip()
    if any(m in u for m in _GOLD_MARKERS):
        ordered = [pos, "GBP", "USD", default]
    elif "USDJPY" in u:
        ordered = [pos, "JPY", "USD", "GBP", default]
    elif any(tok in u for tok in (".EURUSD.", ".GBPUSD.", ".AUDUSD.")):
        ordered = [pos, "USD", "GBP", default]
    elif "FTSE" in u or "DAX" in u:
        ordered = [pos, "GBP", "USD", "EUR", default]
    else:
        ordered = [pos, "USD", default, "GBP"]
    out: list[str] = []
    for c in ordered:
        c = str(c or "").upper()
        if c and c not in out:
            out.append(c)
    return out or ["USD"]


def _rotate_deal_to_back(positions: list, deal_id: str) -> list:
    """Move a stuck deal to the end so the next iteration tries a different open."""
    if not deal_id or len(positions) < 2:
        return positions
    head = []
    moved = None
    for row in positions:
        if moved is None and str(row.get("deal_id") or "") == deal_id:
            moved = row
            continue
        head.append(row)
    if moved is None:
        return positions
    return head + [moved]


def _sort_tradeable_first(positions: list) -> list:
    return sorted(positions, key=lambda r: (_epic_priority(str(r.get("epic") or "")), str(r.get("deal_id") or "")))


def main() -> int:
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from ig_api.endpoints import position_otc
    from runtime import broker_snapshot
    from system.config_loader import get_config
    from system.credentials_loader import load_credentials
    from system.ig_rest_session import ensure_shared_authenticated

    reinforce_pauses()
    cfg = get_config()
    ccy = str(getattr(cfg, "currency_code", None) or "USD")
    rest = ensure_shared_authenticated(load_credentials())

    # Prefer snapshot to start (avoid REST deferral stall); sync soon after.
    snap0 = broker_snapshot.read_snapshot(max_age_sec=None) or {}
    start_n = int(snap0.get("count") or len(snap0.get("positions") or []) or 0)
    log(f"BOOT snapshot_count={start_n} source={snap0.get('source')}")
    items, code = raw_positions(rest)
    if items is not None:
        broker_snapshot.write_snapshot(source="emergency_unwind_start", items=items)
        start_n = len(items)
        log(f"BOOT raw_live={start_n}")
    else:
        log(f"BOOT raw GET deferred/fail http={code} — continuing on snapshot")
    confirmed = 0
    rejected = 0
    errors = 0
    write_progress(
        start_count_reported=203,
        start_count_live=start_n,
        current=start_n,
        target_cap=TARGET_CAP,
        target_ideal=TARGET_IDEAL,
        confirmed=0,
        rejected=0,
        errors=0,
        pause_on=True,
        gap_sec=GAP_SEC,
        method="POST_net_close_confirm_FULLY_CLOSED",
        status="running",
    )
    log(
        f"START live={start_n} target_cap={TARGET_CAP} ideal={TARGET_IDEAL} "
        f"gap={GAP_SEC}s ccy={ccy} pause_on=1"
    )

    since_sync = 0
    rotate_streak: dict[str, int] = {}
    while True:
        reinforce_pauses()
        snap = broker_snapshot.read_snapshot(max_age_sec=None) or {}
        positions = _sort_tradeable_first(list(snap.get("positions") or []))
        n = len(positions)
        write_progress(current=n, confirmed=confirmed, rejected=rejected, errors=errors)

        if n <= TARGET_IDEAL:
            # confirm with raw GET
            live, code = raw_positions(rest)
            if live is not None:
                broker_snapshot.write_snapshot(source="emergency_unwind_flat_check", items=live)
                n = len(live)
            log(f"DONE ideal flat check live={n}")
            write_progress(current=n, status="flat" if n == 0 else "under_ideal_check")
            if n == 0:
                return 0
            positions = _sort_tradeable_first(
                [
                    {
                        "deal_id": str((it.get("position") or {}).get("dealId") or ""),
                        "epic": str((it.get("market") or {}).get("epic") or ""),
                        "direction": str((it.get("position") or {}).get("direction") or "BUY"),
                        "size": float((it.get("position") or {}).get("size") or 0.5),
                    }
                    for it in (live or [])
                ]
            )

        if n <= TARGET_CAP and n > TARGET_IDEAL:
            log(f"UNDER_CAP current={n} — continuing toward flat")

        if not positions:
            time.sleep(GAP_SEC)
            live, code = raw_positions(rest)
            if live is not None:
                broker_snapshot.write_snapshot(source="emergency_unwind_empty_resync", items=live)
                if len(live) == 0:
                    log("FLAT confirmed")
                    write_progress(current=0, status="flat")
                    return 0
            continue

        # Prefer tradeable indices/FX first — metals often MARKET_CLOSED overnight.
        # Skip deals with high rotate streaks so one stuck FX cannot monopolize the loop.
        row = None
        for candidate in positions:
            did = str(candidate.get("deal_id") or "")
            if rotate_streak.get(did, 0) >= 3:
                continue
            row = candidate
            break
        if row is None:
            # All deals are sticky — clear streaks after a longer pause + raw resync.
            log(f"ALL_STICKY n={n} — clearing streaks, raw resync, sleep 60s")
            rotate_streak.clear()
            time.sleep(60)
            live, code = raw_positions(rest)
            if live is not None:
                broker_snapshot.write_snapshot(
                    source="emergency_unwind_sticky_resync", items=live
                )
                log(f"sticky resync raw_live={len(live)}")
                if len(live) == 0:
                    write_progress(current=0, status="flat", confirmed=confirmed)
                    return 0
            continue
        deal_id = str(row.get("deal_id") or "")
        epic = str(row.get("epic") or "IX.D.DOW.IFM.IP")
        side = str(row.get("direction") or "BUY").upper()
        size = float(row.get("size") or 0.5)
        close_dir = "SELL" if side == "BUY" else "BUY"
        position_ccy = str(
            row.get("currency") or row.get("currencyCode") or row.get("ccy") or ""
        ).upper()
        ccy_try_list = _currency_candidates(epic, ccy, position_ccy=position_ccy or None)
        close_ok = False
        last_reason = ""
        last_status = ""
        last_body = ""
        closed_ids: list[str] = []
        for ccy_try in ccy_try_list:
            payload = {
                "epic": epic,
                "expiry": "-",
                "direction": close_dir,
                "size": size,
                "orderType": "MARKET",
                "guaranteedStop": False,
                "forceOpen": False,
                "currencyCode": ccy_try,
            }
            log(
                f"CLOSE attempt open_deal={deal_id[:14]} open_side={side} "
                f"net_dir={close_dir} size={size} ccy={ccy_try} book={n} epic={epic[:28]}"
            )
            try:
                r = rest.request(
                    "POST",
                    position_otc(),
                    headers=rest._auth_headers("2"),
                    json=payload,
                    budget_priority=True,
                )
                last_body = (r.text or "")[:300]
                if r.status_code not in (200, 201):
                    errors += 1
                    log(f"  HTTP {r.status_code} {last_body}")
                    if "allowance" in last_body.lower() or r.status_code == 403:
                        log("  ALLOWANCE — sleeping 90s")
                        time.sleep(90)
                        break
                    continue
                ref = (r.json() or {}).get("dealReference")
                conf = rest.confirm_deal(ref) if ref else {}
                raw = conf.get("raw") if isinstance(conf, dict) else {}
                last_status = str(
                    (raw or {}).get("dealStatus")
                    or conf.get("status")
                    or ""
                ).upper()
                last_reason = str((raw or {}).get("reason") or conf.get("reason") or "")
                affected = list((raw or {}).get("affectedDeals") or [])
                closed_ids = []
                for a in affected:
                    if not isinstance(a, dict):
                        continue
                    st = str(a.get("status") or "").upper()
                    did = str(a.get("dealId") or a.get("deal_id") or "").strip()
                    if did and st in ("FULLY_CLOSED", "CLOSED", "DELETED"):
                        closed_ids.append(did)
                conf_deal = str(conf.get("deal_id") or (raw or {}).get("dealId") or "").strip()
                # OPENED = spawn (new deal) — never count as close success.
                if last_status == "OPENED" or conf.get("opened") is True:
                    rejected += 1
                    log(
                        f"  SPAWN_OPENED rejected status={last_status} reason={last_reason} "
                        f"raw_deal={conf_deal}"
                    )
                    break
                accepted = bool(conf.get("accepted")) or last_status in (
                    "ACCEPTED",
                    "FULLY_CLOSED",
                    "CLOSED",
                    "SUCCESS",
                )
                if accepted and last_status not in ("REJECTED",):
                    # Never treat confirm.dealId alone as a close — on DEMO that is
                    # often the newly opened hedge deal while the target BUY stays open.
                    target_listed = deal_id in closed_ids
                    # Capture live count — empty affected + ACCEPTED can still SPAWN.
                    live_before_n = n
                    # IG net-close (forceOpen=false) closes a matching open by FIFO —
                    # affected FULLY_CLOSED may be a *different* dealId than the one
                    # we aimed at. For cascade flatten that still reduces the book.
                    if closed_ids:
                        drop_ids = list(closed_ids)
                        # Also drop the aimed deal if IG closed it under another id shape.
                        if deal_id and deal_id not in drop_ids and target_listed:
                            drop_ids.append(deal_id)
                        broker_snapshot.remove_deals_from_snapshot(
                            drop_ids, source="emergency_unwind_affected"
                        )
                        confirmed += 1
                        since_sync += 1
                        for did in drop_ids:
                            rotate_streak.pop(did, None)
                        rotate_streak.pop(deal_id, None)
                        snap_n = (broker_snapshot.read_snapshot(max_age_sec=None) or {}).get(
                            "count"
                        )
                        log(
                            f"  CONFIRMED {last_status}/{last_reason} aimed={deal_id} "
                            f"listed={target_listed} affected={closed_ids} "
                            f"snap_count={snap_n} confirmed_total={confirmed}"
                        )
                        close_ok = True
                        break
                    # Ambiguous ACCEPTED without FULLY_CLOSED — rare raw verify (budget costly).
                    live_after, live_code = raw_positions(rest)
                    target_still_open = True
                    live_after_n = None
                    if live_after is not None:
                        live_after_n = len(live_after)
                        open_ids = {
                            str((it.get("position") or {}).get("dealId") or "").strip()
                            for it in live_after
                        }
                        target_still_open = deal_id in open_ids
                        broker_snapshot.write_snapshot(
                            source="emergency_unwind_post_close", items=live_after
                        )
                        if closed_ids:
                            broker_snapshot.remove_deals_from_snapshot(
                                closed_ids, source="emergency_unwind_affected"
                            )
                    # Spawn guard: book grew → reject even if target vanished.
                    if live_after_n is not None and live_after_n > int(live_before_n):
                        rejected += 1
                        log(
                            f"  SPAWN_COUNT_UP {live_before_n}->{live_after_n} "
                            f"status={last_status} target={deal_id} — not a close"
                        )
                        break
                    if not target_still_open and (
                        live_after_n is None or live_after_n < int(live_before_n)
                    ):
                        confirmed += 1
                        since_sync += 1
                        rotate_streak.pop(deal_id, None)
                        snap_n = (broker_snapshot.read_snapshot(max_age_sec=None) or {}).get(
                            "count"
                        )
                        log(
                            f"  CONFIRMED_RAW {last_status}/{last_reason} target={deal_id} "
                            f"listed={target_listed} affected={closed_ids} "
                            f"snap_count={snap_n} confirmed_total={confirmed}"
                        )
                        close_ok = True
                        break
                    rejected += 1
                    rotate_streak[deal_id] = int(rotate_streak.get(deal_id, 0)) + 1
                    log(
                        f"  ACCEPTED_TARGET_STILL_OPEN status={last_status} "
                        f"reason={last_reason} target={deal_id} conf_deal={conf_deal} "
                        f"affected={closed_ids} live_http={live_code} "
                        f"streak={rotate_streak[deal_id]}"
                    )
                    break
                rejected += 1
                log(
                    f"  REJECTED status={last_status} reason={last_reason} "
                    f"ccy={ccy_try} body={last_body[:120]}"
                )
                # Currency mismatch — try next candidate immediately.
                if "INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY" in last_reason.upper():
                    continue
                # Market closed / soft blocks — stop currency loop, rotate deal.
                if any(tok in last_reason.upper() for tok in _ROTATE_REASONS):
                    break
                if "allowance" in last_reason.lower() or "allowance" in last_body.lower():
                    log("  ALLOWANCE — sleeping 90s")
                    time.sleep(90)
                    break
            except Exception as exc:
                errors += 1
                log(f"  ERR {type(exc).__name__}: {exc}")
                if "allowance" in str(exc).lower():
                    time.sleep(90)
                    break

        if not close_ok and deal_id:
            streak = rotate_streak.get(deal_id, 0) + 1
            rotate_streak[deal_id] = streak
            reason_u = last_reason.upper()
            if any(tok in reason_u for tok in _ROTATE_REASONS) or streak >= 2:
                rotated = _rotate_deal_to_back(positions, deal_id)
                try:
                    broker_snapshot.write_snapshot(
                        source="emergency_unwind_rotate",
                        positions=rotated,
                    )
                except TypeError:
                    # Older write_snapshot signature uses items=
                    try:
                        broker_snapshot.write_snapshot(
                            source="emergency_unwind_rotate",
                            items=[
                                {
                                    "position": {
                                        "dealId": r.get("deal_id"),
                                        "direction": r.get("direction"),
                                        "size": r.get("size"),
                                    },
                                    "market": {"epic": r.get("epic")},
                                    "_normalized": r,
                                }
                                for r in rotated
                            ],
                        )
                    except Exception as exc:
                        log(f"  rotate snapshot write failed: {exc}")
                else:
                    log(
                        f"  ROTATE deal={deal_id[:14]} epic={epic[:24]} "
                        f"streak={streak} reason={last_reason or last_status}"
                    )
                # If only uncloseable residue remains, pause longer.
                if all(
                    _epic_priority(str(p.get("epic") or "")) >= 90 for p in rotated
                ) and len(rotated) <= 6:
                    log(
                        f"  RESIDUE_METALS_ONLY n={len(rotated)} — sleep 120s "
                        f"(await market open / edits)"
                    )
                    time.sleep(120)
                    live, code = raw_positions(rest)
                    if live is not None:
                        broker_snapshot.write_snapshot(
                            source="emergency_unwind_metals_resync", items=live
                        )
                        log(f"  metals resync raw_live={len(live)}")
                        if len(live) == 0:
                            write_progress(current=0, status="flat")
                            return 0

        write_progress(
            current=(broker_snapshot.read_snapshot(max_age_sec=None) or {}).get("count"),
            confirmed=confirmed,
            rejected=rejected,
            errors=errors,
            status="running",
        )

        if since_sync >= SYNC_EVERY:
            time.sleep(max(5.0, GAP_SEC * 0.5))
            live, code = raw_positions(rest)
            if live is not None:
                broker_snapshot.write_snapshot(source="emergency_unwind_sync", items=live)
                log(f"SYNC raw_live={len(live)}")
                since_sync = 0
            else:
                log(f"SYNC deferred/fail http={code}")

        time.sleep(GAP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
