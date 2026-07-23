"""
Surgical stale-position reconciler — isolated IG REST repair (no Lightstreamer).

Pulls broker-truth for one dealId, repopulates local entry/currency caches, and
computes synthetic GBP PnL when streaming marks are missing.

CLI (from repo root)::

  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    PYTHONPATH=src python3 -m execution.reconcile_stale_position \\
    --deal-id DIAAAAXY5H2RZAR

  # Dry-run (no disk writes):
  PYTHONPATH=src python3 -m execution.reconcile_stale_position \\
    --deal-id DIAAAAXY5H2RZAR --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Allow ``python src/execution/reconcile_stale_position.py`` as well as -m
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

DEFAULT_DEAL_ID = "DIAAAAXY5H2RZAR"


def extract_broker_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize IG ``/positions`` or ``/positions/otc/{id}`` payload."""
    pos = item.get("position") if isinstance(item.get("position"), dict) else {}
    mkt = item.get("market") if isinstance(item.get("market"), dict) else {}
    # Some OTC GETs nest differently
    if not pos and item.get("dealId"):
        pos = item
    deal_id = str(
        pos.get("dealId") or pos.get("dealID") or item.get("dealId") or ""
    ).strip()
    epic = str(mkt.get("epic") or pos.get("epic") or item.get("epic") or "").strip()
    direction = str(pos.get("direction") or item.get("direction") or "BUY").upper()
    try:
        level = float(
            pos.get("level")
            or pos.get("openLevel")
            or item.get("level")
            or item.get("openLevel")
            or 0
        )
    except (TypeError, ValueError):
        level = 0.0
    try:
        size = float(pos.get("size") or item.get("size") or 0)
    except (TypeError, ValueError):
        size = 0.0
    currency = str(
        pos.get("currency")
        or mkt.get("currency")
        or item.get("currency")
        or ""
    ).upper()
    bid = float(mkt.get("bid") or item.get("bid") or 0)
    offer = float(mkt.get("offer") or item.get("offer") or 0)
    upl = None
    for key in ("profitAndLoss", "upl", "unrealizedPNL"):
        raw = pos.get(key) if key in pos else item.get(key)
        if raw is None:
            continue
        try:
            # IG may return "U123.45" style
            from system.ig_money import parse_ig_money

            amt, ccy = parse_ig_money(raw)
            if amt is not None:
                upl = float(amt)
                if ccy:
                    currency = str(ccy).upper()
                break
        except Exception:
            try:
                upl = float(raw)
                break
            except (TypeError, ValueError):
                continue
    return {
        "deal_id": deal_id,
        "epic": epic,
        "direction": direction,
        "entry": level,
        "size": size,
        "currency": currency or "GBP",
        "bid": bid,
        "offer": offer,
        "upl": upl,
        "raw_position": pos,
        "raw_market": mkt,
    }


def extract_from_activity_or_txn(
    rows: list[dict[str, Any]], deal_id: str
) -> dict[str, Any] | None:
    """Scan history/activity or transactions for dealId → level/currency."""
    want = str(deal_id or "").strip().upper()
    if not want:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        blob = json.dumps(row, default=str).upper()
        if want not in blob:
            continue
        # Activity details often nest under details / channel
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        level = (
            row.get("level")
            or row.get("openLevel")
            or details.get("level")
            or details.get("openLevel")
            or row.get("price")
        )
        currency = (
            row.get("currency")
            or details.get("currency")
            or row.get("currencyCode")
            or ""
        )
        mkt = row.get("market") if isinstance(row.get("market"), dict) else {}
        epic = str(
            row.get("epic")
            or details.get("epic")
            or mkt.get("epic")
            or row.get("instrument")
            or ""
        ).strip()
        direction = str(
            row.get("direction") or details.get("direction") or "BUY"
        ).upper()
        size = float(row.get("size") or details.get("size") or 0)
        try:
            entry = float(level or 0)
        except (TypeError, ValueError):
            entry = 0.0
        if entry <= 0:
            continue
        return {
            "deal_id": str(deal_id).strip(),
            "epic": epic,
            "direction": direction if direction in ("BUY", "SELL") else "BUY",
            "entry": entry,
            "size": size,
            "currency": str(currency or "GBP").upper(),
            "bid": 0.0,
            "offer": 0.0,
            "upl": None,
            "source": "history",
        }
    return None


def synthetic_pnl_gbp(
    *,
    epic: str,
    direction: str,
    entry: float,
    size: float,
    bid: float,
    offer: float,
    currency: str,
    mid: float | None = None,
) -> float | None:
    """Entry vs mark → GBP; prefers bid/offer, falls back to mid."""
    from execution.position_pnl_gbp import pnl_gbp_for_open_row

    b = float(bid or 0)
    o = float(offer or 0)
    if (b <= 0 or o <= 0) and mid is not None and float(mid) > 0:
        m = float(mid)
        # Symmetric 1-tick synthetic book around mid for mark math
        tick = max(0.5, abs(m) * 1e-6)
        b, o = m - tick, m + tick
    return pnl_gbp_for_open_row(
        epic=epic,
        direction=direction,
        entry_level=float(entry),
        size=float(size),
        upl=None,
        bid=b,
        offer=o,
        currency=currency,
    )


def _mid_from_hub_or_rest(rest: Any, epic: str) -> tuple[float, float, float]:
    """Return (bid, offer, mid) — hub first, then REST market snapshot."""
    bid = offer = 0.0
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(epic)
        if snap is not None:
            bid = float(getattr(snap, "bid", 0) or 0)
            offer = float(getattr(snap, "offer", 0) or 0)
    except Exception:
        pass
    if bid <= 0 or offer <= bid:
        try:
            if hasattr(rest, "fetch_market_snapshot"):
                m = rest.fetch_market_snapshot(epic) or {}
                bid = float(m.get("bid") or m.get("Bid") or 0)
                offer = float(m.get("offer") or m.get("Offer") or 0)
            elif hasattr(rest, "fetch_live_prices"):
                m = rest.fetch_live_prices(epic) or {}
                bid = float(m.get("bid") or 0)
                offer = float(m.get("offer") or 0)
        except Exception:
            pass
    mid = (bid + offer) / 2.0 if bid > 0 and offer > bid else 0.0
    return bid, offer, mid


def fetch_broker_truth(rest: Any, deal_id: str) -> dict[str, Any]:
    """Isolated REST pull — position OTC → open list → activity/transactions."""
    want = str(deal_id).strip()
    sources: list[str] = []

    item: dict[str, Any] | None = None
    if hasattr(rest, "get_position_otc"):
        try:
            item = rest.get_position_otc(want)
            if item:
                sources.append("GET /positions/otc/{dealId}")
        except Exception as exc:
            sources.append(f"get_position_otc_error:{type(exc).__name__}")

    if item is None and hasattr(rest, "find_open_position"):
        try:
            item = rest.find_open_position(want)
            if item:
                sources.append("GET /positions scan")
        except Exception as exc:
            sources.append(f"find_open_error:{type(exc).__name__}")

    fields: dict[str, Any] | None = None
    if isinstance(item, dict):
        fields = extract_broker_fields(item)
        if float(fields.get("entry") or 0) <= 0:
            fields = None

    if fields is None:
        # Journal fallback — last 14 days of activity / transactions
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=14)
        from_s = start.strftime("%Y-%m-%d")
        to_s = end.strftime("%Y-%m-%d")
        activities: list[dict[str, Any]] = []
        txns: list[dict[str, Any]] = []
        try:
            if hasattr(rest, "fetch_account_activity"):
                activities = list(rest.fetch_account_activity(from_s, to_s) or [])
                sources.append(f"history/activity n={len(activities)}")
        except Exception as exc:
            sources.append(f"activity_error:{type(exc).__name__}")
        try:
            if hasattr(rest, "fetch_transactions"):
                txns = list(
                    rest.fetch_transactions(from_s, to_s, transaction_type="ALL_DEAL")
                    or []
                )
                sources.append(f"history/transactions n={len(txns)}")
        except Exception as exc:
            sources.append(f"txn_error:{type(exc).__name__}")
        hist = extract_from_activity_or_txn(activities + txns, want)
        if hist:
            fields = hist
            sources.append("history_match")

    if fields is None:
        return {
            "ok": False,
            "deal_id": want,
            "error": "deal_not_found_on_broker",
            "sources": sources,
        }

    fields["sources"] = sources
    fields["ok"] = True
    return fields


def repair_local_caches(
    truth: dict[str, Any],
    *,
    pnl_gbp: float | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Overwrite broker_snapshot + learning DB + repair sidecar."""
    from system.paths import data_dir

    deal_id = str(truth["deal_id"])
    actions: list[str] = []

    # 1) Shared broker snapshot (cross-process SoT for positions/live)
    try:
        from runtime import broker_snapshot

        snap = broker_snapshot.read_snapshot(max_age_sec=None) or {
            "positions": [],
            "ts": 0,
            "source": "reconcile_stale",
            "count": 0,
        }
        positions = list(snap.get("positions") or [])
        found = False
        for row in positions:
            if str(row.get("deal_id") or "") == deal_id:
                row["entry"] = float(truth["entry"])
                row["epic"] = truth.get("epic") or row.get("epic")
                row["direction"] = truth.get("direction") or row.get("direction")
                row["size"] = float(truth.get("size") or row.get("size") or 0)
                row["currency"] = truth.get("currency") or row.get("currency")
                row["pnl_gbp"] = (
                    round(float(pnl_gbp), 2) if pnl_gbp is not None else row.get("pnl_gbp")
                )
                row["stale"] = False
                row["repaired_at"] = time.time()
                row["repair_source"] = "reconcile_stale_position"
                found = True
                break
        if not found:
            positions.append(
                {
                    "deal_id": deal_id,
                    "epic": truth.get("epic"),
                    "direction": truth.get("direction"),
                    "size": float(truth.get("size") or 0),
                    "entry": float(truth["entry"]),
                    "currency": truth.get("currency"),
                    "pnl_gbp": round(float(pnl_gbp), 2) if pnl_gbp is not None else None,
                    "stale": False,
                    "repaired_at": time.time(),
                    "repair_source": "reconcile_stale_position",
                }
            )
        if not dry_run:
            broker_snapshot.write_snapshot(
                source="reconcile_stale_position",
                positions=positions,
            )
        actions.append("broker_snapshot_updated" if not dry_run else "broker_snapshot_dry")
    except Exception as exc:
        actions.append(f"broker_snapshot_error:{type(exc).__name__}:{exc}")

    # 2) Learning DB open row entry
    try:
        from data.learning_store import LearningStore

        store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
        open_row = store.find_open_by_deal_id(deal_id)
        if open_row is not None:
            if not dry_run:
                store.update_open_entry(
                    deal_id,
                    float(truth["entry"]),
                    currency=str(truth.get("currency") or ""),
                )
            actions.append(
                "learning_db_entry_patched" if not dry_run else "learning_db_dry"
            )
        else:
            actions.append("learning_db_no_open_row")
    except Exception as exc:
        actions.append(f"learning_db_error:{type(exc).__name__}:{exc}")

    # 3) Repair sidecar — explicit operator/GUI enrichment file
    repair_path = data_dir() / "state" / "stale_position_repair.json"
    payload = {
        "ts": time.time(),
        "stale": False,
        "deal_id": deal_id,
        "entry": float(truth["entry"]),
        "currency": truth.get("currency"),
        "epic": truth.get("epic"),
        "direction": truth.get("direction"),
        "size": truth.get("size"),
        "pnl_gbp": round(float(pnl_gbp), 2) if pnl_gbp is not None else None,
        "pnl_source": "synthetic_mid" if pnl_gbp is not None else None,
        "sources": truth.get("sources"),
    }
    if not dry_run:
        repair_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = repair_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, repair_path)
    actions.append("repair_sidecar_written" if not dry_run else "repair_sidecar_dry")

    # 4) Best-effort in-process GBP track re-arm (no-op if engine not in this PID)
    try:
        from runtime.micro_gbp_exit import register_gbp_exit, snapshot as gbp_snap

        tracks = (gbp_snap().get("tracks") or {}) if callable(gbp_snap) else {}
        prev = tracks.get(deal_id) or {}
        if not dry_run and float(truth.get("entry") or 0) > 0:
            register_gbp_exit(
                deal_id=deal_id,
                epic=str(truth.get("epic") or ""),
                direction=str(truth.get("direction") or "BUY"),
                size=float(truth.get("size") or prev.get("size") or 0.5),
                entry_level=float(truth["entry"]),
                loss_cap_gbp=float(prev.get("loss_cap_gbp") or 4.0),
                target_profit_gbp=float(prev.get("target_profit_gbp") or 8.0),
                trail_trigger_gbp=float(prev.get("trail_trigger_gbp") or 2.0),
                soft_loss_gbp=float(prev.get("soft_loss_gbp") or 2.2),
            )
            actions.append("gbp_track_rearmed")
    except Exception as exc:
        actions.append(f"gbp_track_skip:{type(exc).__name__}")

    return {"actions": actions, "repair_path": str(repair_path), "payload": payload}


def reconcile_stale_position(
    deal_id: str = DEFAULT_DEAL_ID,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """End-to-end repair for one dealId."""
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import ensure_shared_authenticated

    cred = try_load_credentials()
    if not cred.ok or not cred.credentials:
        return {"ok": False, "error": "credentials_unavailable", "deal_id": deal_id}

    rest = ensure_shared_authenticated(cred.credentials)
    truth = fetch_broker_truth(rest, deal_id)
    if not truth.get("ok"):
        # Flat / missing deal — deactivate emergency snapshot mirror if book is clear.
        try:
            from runtime.snapshot_mirror_cleanup import (
                maybe_deactivate_legacy_snapshot_mirror,
            )

            truth = dict(truth)
            truth["mirror_cleanup"] = maybe_deactivate_legacy_snapshot_mirror()
        except Exception:
            pass
        return truth

    entry = float(truth["entry"])
    if entry <= 0:
        return {
            "ok": False,
            "deal_id": deal_id,
            "error": "broker_entry_still_zero",
            "truth": truth,
        }

    epic = str(truth.get("epic") or "")
    bid, offer, mid = _mid_from_hub_or_rest(rest, epic)
    if bid > 0 and offer > bid:
        truth["bid"] = bid
        truth["offer"] = offer

    pnl = None
    if truth.get("upl") is not None:
        try:
            from trading.open_position_view import pnl_currency_amount_to_gbp

            pnl = float(
                pnl_currency_amount_to_gbp(
                    float(truth["upl"]), str(truth.get("currency") or "GBP")
                )
            )
            truth["pnl_source"] = "broker_upl"
        except Exception:
            pnl = None

    if pnl is None:
        pnl = synthetic_pnl_gbp(
            epic=epic,
            direction=str(truth.get("direction") or "BUY"),
            entry=entry,
            size=float(truth.get("size") or 0),
            bid=float(truth.get("bid") or 0),
            offer=float(truth.get("offer") or 0),
            currency=str(truth.get("currency") or "GBP"),
            mid=mid if mid > 0 else None,
        )
        truth["pnl_source"] = "synthetic_mid"

    repair = repair_local_caches(truth, pnl_gbp=pnl, dry_run=dry_run)
    return {
        "ok": True,
        "deal_id": deal_id,
        "stale": False,
        "entry": entry,
        "currency": truth.get("currency"),
        "epic": epic,
        "direction": truth.get("direction"),
        "size": truth.get("size"),
        "mid": mid,
        "pnl_gbp": round(float(pnl), 2) if pnl is not None else None,
        "pnl_source": truth.get("pnl_source"),
        "sources": truth.get("sources"),
        "repair": repair,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Surgical IG REST reconciler for stale open positions"
    )
    parser.add_argument(
        "--deal-id",
        default=os.environ.get("IG_REPAIR_DEAL_ID", DEFAULT_DEAL_ID),
        help=f"Broker dealId (default {DEFAULT_DEAL_ID})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + compute only — do not write caches",
    )
    args = parser.parse_args(argv)
    result = reconcile_stale_position(args.deal_id, dry_run=bool(args.dry_run))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
