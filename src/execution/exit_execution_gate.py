"""Single exit execution state machine — one flatten authority for the desk.

All soft/hard/trail/hard-floor/OPM exits route through ``request_flatten``.
Atomic ``is_executing`` lock per deal prevents Micro ↔ OPM double-fires.
Trackers are paused before the close order; removed only after confirmed close
or edits-only enqueue.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

_lock = threading.RLock()
_executing: set[str] = set()
_paused: set[str] = set()
_last_result: dict[str, Any] = {}
_emergency_kill_active = False

# Per-deal flatten circuit — stop retry spam after N failures.
_FLATTEN_FAIL_MAX = 3
_FLATTEN_COOLDOWN_SEC = 300.0
_flatten_fail_counts: dict[str, int] = {}
_flatten_circuit_until: dict[str, float] = {}

# Close-path confirm statuses. OPENED means a new position was spawned — never success.
_CLOSE_CONFIRM_OK = frozenset({"FULLY_CLOSED", "CLOSED", "DELETED"})
_CLOSE_CONFIRM_SPAWN = frozenset({"OPENED", "PARTIALLY_OPENED"})
_CLOSE_CONFIRM_REJECT = frozenset({"REJECTED", "CANCELLED", "TIMEOUT"})


def _confirm_deal_status(confirm: dict[str, Any]) -> str:
    raw = confirm.get("raw") if isinstance(confirm.get("raw"), dict) else {}
    return str(
        confirm.get("dealStatus")
        or confirm.get("status")
        or (raw or {}).get("dealStatus")
        or (raw or {}).get("status")
        or ""
    ).upper()


def _close_confirm_verdict(confirm: dict[str, Any] | None) -> tuple[bool, str, str]:
    """Return (ok_for_close, status, error_code).

    A close path must NEVER treat OPENED as success — that is a net-close spawn.
    ``accepted``/``terminal`` alone are insufficient (opens also return those).
    """
    if not isinstance(confirm, dict):
        return False, "", "missing_confirm"
    status = _confirm_deal_status(confirm)
    if status in _CLOSE_CONFIRM_SPAWN or confirm.get("opened") is True:
        return False, status or "OPENED", "close_confirm_opened_spawn"
    if status in _CLOSE_CONFIRM_REJECT or confirm.get("rejected") is True:
        return False, status or "REJECTED", "close_confirm_rejected"
    if status in _CLOSE_CONFIRM_OK:
        return True, status, ""
    # ACCEPTED is ambiguous (entry vs close) — not proof of flatten by itself.
    return False, status, "close_confirm_ambiguous"


def is_executing(deal_id: str | None = None) -> bool:
    with _lock:
        if deal_id:
            return str(deal_id) in _executing
        return bool(_executing)


def is_paused(deal_id: str) -> bool:
    with _lock:
        return str(deal_id) in _paused or str(deal_id) in _executing


def emergency_kill_active() -> bool:
    with _lock:
        return _emergency_kill_active


def set_emergency_kill_active(active: bool) -> None:
    global _emergency_kill_active
    with _lock:
        _emergency_kill_active = bool(active)


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "executing": sorted(_executing),
            "paused": sorted(_paused),
            "emergency_kill": _emergency_kill_active,
            "last_result": dict(_last_result),
        }


def _pause_trackers(deal_id: str) -> None:
    """Disarm evaluation loops for this deal BEFORE the close hits the wire."""
    did = str(deal_id or "").strip()
    if not did:
        return
    with _lock:
        _paused.add(did)
    try:
        from runtime import micro_gbp_exit as mge

        # Soft-disarm: mark in-flight so micro will not schedule a parallel flatten.
        with mge._lock:
            mge._in_flight.add(did)
    except Exception:
        pass


def _resume_trackers_on_failure(deal_id: str) -> None:
    did = str(deal_id or "").strip()
    with _lock:
        _paused.discard(did)
    try:
        from runtime import micro_gbp_exit as mge

        with mge._lock:
            mge._in_flight.discard(did)
    except Exception:
        pass


def _remove_trackers(deal_id: str) -> None:
    did = str(deal_id or "").strip()
    with _lock:
        _paused.discard(did)
    try:
        from runtime.micro_gbp_exit import remove_track

        remove_track(did)
    except Exception:
        pass
    try:
        from runtime.dynamic_limit_engine import remove_track as remove_dyn
        from runtime.virtual_stop_loss import clear_virtual_stop

        clear_virtual_stop(did)
        remove_dyn(did)
    except Exception:
        pass
    try:
        from runtime import micro_gbp_exit as mge

        with mge._lock:
            mge._in_flight.discard(did)
    except Exception:
        pass


def _reconcile_book(rest: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False}
    try:
        raw = rest.open_positions(budget_priority=True)
        items = list(raw) if isinstance(raw, (list, tuple)) else []
        out = {
            "ok": True,
            "broker_open": len(items),
            "deal_ids": [
                str((it.get("position") or {}).get("dealId") or "").strip()
                for it in items
                if isinstance(it, dict)
                and str((it.get("position") or {}).get("dealId") or "").strip()
            ],
        }
        try:
            from runtime import broker_snapshot

            broker_snapshot.write_snapshot(source="exit_execution_gate", items=items)
        except Exception:
            pass
    except Exception as exc:
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def flatten_circuit_open(deal_id: str) -> bool:
    did = str(deal_id or "").strip()
    if not did:
        return False
    with _lock:
        until = float(_flatten_circuit_until.get(did) or 0.0)
        if until <= 0:
            return False
        if time.time() < until:
            return True
        _flatten_circuit_until.pop(did, None)
        _flatten_fail_counts.pop(did, None)
        return False


def _record_flatten_failure(deal_id: str, *, detail: str = "") -> None:
    did = str(deal_id or "").strip()
    if not did:
        return
    with _lock:
        n = int(_flatten_fail_counts.get(did) or 0) + 1
        _flatten_fail_counts[did] = n
        if n >= _FLATTEN_FAIL_MAX:
            _flatten_circuit_until[did] = time.time() + _FLATTEN_COOLDOWN_SEC
            log_engine(
                f"ExitGate: flatten circuit OPEN deal={did[:12]} fails={n} "
                f"cooldown={_FLATTEN_COOLDOWN_SEC:.0f}s {detail}"
            )


def _clear_flatten_failure(deal_id: str) -> None:
    did = str(deal_id or "").strip()
    with _lock:
        _flatten_fail_counts.pop(did, None)
        _flatten_circuit_until.pop(did, None)


def request_flatten(
    *,
    rest: Any,
    deal_id: str,
    epic: str,
    direction: str = "BUY",
    size: float = 0.0,
    reason: str = "",
    pnl_gbp: float | None = None,
    cfg: Any | None = None,
    source: str = "exit_gate",
    hold_sec: float | None = None,
    style: str | None = None,
) -> dict[str, Any]:
    """Atomic flatten — pause trackers, close, reconcile. One in-flight per deal."""
    global _last_result
    did = str(deal_id or "").strip()
    if not did:
        return {"ok": False, "error": "missing_deal_id", "source": source}
    _hold_sec_hint = hold_sec
    _style_hint = style

    if flatten_circuit_open(did):
        return {
            "ok": False,
            "skipped": True,
            "reason": "flatten_circuit_open",
            "deal_id": did,
            "source": source,
        }

    with _lock:
        if did in _executing:
            return {
                "ok": False,
                "skipped": True,
                "reason": "is_executing",
                "deal_id": did,
                "source": source,
            }
        _executing.add(did)

    result: dict[str, Any] = {
        "ok": False,
        "deal_id": did,
        "epic": str(epic or ""),
        "reason": str(reason or "")[:200],
        "source": source,
        "pnl_gbp": pnl_gbp,
    }
    try:
        _pause_trackers(did)
        if rest is None:
            result["error"] = "no_rest_client"
            _resume_trackers_on_failure(did)
            return result

        direction_u = str(direction or "BUY").upper()
        size_f = float(size or 0)
        # Refresh size/direction from live book when possible.
        book_checked = False
        found_in_book = False
        try:
            raw_book = rest.open_positions(budget_priority=True)
            items = list(raw_book) if isinstance(raw_book, (list, tuple)) else []
            book_checked = True
            for item in items:
                if not isinstance(item, dict):
                    continue
                pos = item.get("position") or {}
                mid = str(pos.get("dealId") or pos.get("dealID") or "").strip()
                if mid != did:
                    continue
                found_in_book = True
                direction_u = str(pos.get("direction") or direction_u).upper()
                size_f = float(pos.get("size") or size_f or 0)
                mkt = item.get("market") or {}
                if mkt.get("epic"):
                    result["epic"] = str(mkt.get("epic"))
                break
            if book_checked and not found_in_book and isinstance(raw_book, (list, tuple)):
                # Confirmed empty / absent — already flat.
                _remove_trackers(did)
                _clear_flatten_failure(did)
                result["ok"] = True
                result["already_flat"] = True
                result["reconcile"] = _reconcile_book(rest)
                return result
        except Exception as exc:
            result["book_refresh_error"] = f"{type(exc).__name__}: {exc}"

        if size_f <= 0:
            result["error"] = "invalid_size"
            _record_flatten_failure(did, detail="invalid_size")
            _resume_trackers_on_failure(did)
            return result

        # close_position(skip_lookup=True) inverts OPEN side once — pass OPEN
        # direction here. Passing close_dir double-inverts and leaves the deal open.
        epic_s = str(result.get("epic") or epic or "")
        log_engine(
            f"ExitGate: FLATTEN deal={did[:12]} epic={epic_s} "
            f"src={source} — {reason}"
        )
        close_data = rest.close_position(
            did,
            direction=direction_u,
            size=size_f,
            epic=epic_s or None,
            # verify=True required — with verify=False net-close sets
            # verified_closed=True even when the deal remains open, which
            # defeats the flatten circuit-breaker and re-arms spam.
            verify=True,
            budget_priority=True,
            skip_lookup=True,
            skip_confirm=False,
        )
        if isinstance(close_data, dict) and close_data.get("skipped"):
            result["ok"] = False
            result["skipped"] = True
            result["error"] = str(close_data.get("reason") or "exit_inflight")
            _resume_trackers_on_failure(did)
            return result
        confirm = (close_data or {}).get("confirm") if isinstance(close_data, dict) else None
        confirm_ok, status, confirm_err = _close_confirm_verdict(
            confirm if isinstance(confirm, dict) else None
        )
        if isinstance(confirm, dict):
            result["confirm"] = {
                "dealStatus": status or confirm.get("status"),
                "dealId": confirm.get("deal_id") or confirm.get("dealId"),
                "accepted": confirm.get("accepted"),
            }
        # Hard abort: net-close that OPENED a new deal must never look like success,
        # even if the original dealId is absent (ghost/dead-deal spawn).
        if confirm_err == "close_confirm_opened_spawn" or (
            isinstance(close_data, dict) and close_data.get("close_spawned")
        ):
            result["ok"] = False
            result["error"] = "close_confirm_opened_spawn"
            result["close_data"] = {
                k: close_data.get(k)
                for k in ("dealReference", "verified_closed", "confirm", "close_spawned")
                if isinstance(close_data, dict)
            }
            _record_flatten_failure(did, detail="close_confirm_opened_spawn")
            _resume_trackers_on_failure(did)
            log_engine(
                f"ExitGate: ABORT spawn OPENED deal={did[:12]} "
                f"status={status or 'OPENED'} — close must not succeed"
            )
            return result

        confirmed = bool(confirm_ok)
        # verified_closed is only trusted when confirm is not a spawn and not rejected.
        if (
            isinstance(close_data, dict)
            and close_data.get("verified_closed")
            and confirm_err not in ("close_confirm_opened_spawn", "close_confirm_rejected")
        ):
            # ACCEPTED + verified_closed (original deal gone, no OPENED spawn) is OK.
            confirmed = True
        # dealReference alone is NOT proof of flatten — IG net-close often returns
        # 200+ref while the deal stays open (wrong-dir / validation fallback).
        if not confirmed and isinstance(close_data, dict) and close_data.get("dealReference"):
            # Re-check book before treating as closed — only if confirm is not a spawn.
            if confirm_err != "close_confirm_opened_spawn":
                try:
                    still = False
                    for item in rest.open_positions(budget_priority=True) or []:
                        pos = (item or {}).get("position") or {}
                        if str(pos.get("dealId") or pos.get("dealID") or "").strip() == did:
                            still = True
                            break
                    # ACCEPTED/SUCCESS alone is ambiguous for entries, but when the
                    # original dealId is gone from the book the flatten succeeded.
                    if not still and (
                        confirm_ok
                        or status in _CLOSE_CONFIRM_OK
                        or status in ("ACCEPTED", "SUCCESS", "OK", "FILLED")
                        or confirm_err == "close_confirm_ambiguous"
                        or (
                            isinstance(close_data, dict)
                            and close_data.get("verified_closed")
                        )
                    ):
                        confirmed = True
                        result["confirm"] = {
                            "dealReference": close_data.get("dealReference"),
                            "book_absent": True,
                            "dealStatus": status or None,
                        }
                except Exception:
                    pass
        if not confirmed:
            result["ok"] = False
            result["error"] = confirm_err or "close_not_confirmed"
            result["close_data"] = {
                k: close_data.get(k)
                for k in ("dealReference", "verified_closed", "confirm", "close_spawned")
                if isinstance(close_data, dict)
            }
            _record_flatten_failure(did, detail=result["error"])
            _resume_trackers_on_failure(did)
            return result
        _remove_trackers(did)
        _clear_flatten_failure(did)
        try:
            from runtime.broker_snapshot import remove_deals_from_snapshot

            remove_deals_from_snapshot([did], source="exit_gate_confirmed")
        except Exception:
            pass
        result["ok"] = True
        result["reconcile"] = _reconcile_book(rest)
        try:
            from runtime.strategy_improvement_tracker import record_managed_close

            record_managed_close(
                epic=epic_s,
                pnl_gbp=float(pnl_gbp if pnl_gbp is not None else 0.0),
                exit_reason=f"{source}:{reason}"[:160],
            )
        except Exception:
            pass
        # Hot-path micro flattens bypass learning_store — still arm the cash journal
        # so soak / milestone trackers see real DIAAAA DealIDs.
        try:
            from diagnostics.performance_journal import record_trade_close

            conf = confirm if isinstance(confirm, dict) else {}
            raw = conf.get("raw") if isinstance(conf.get("raw"), dict) else {}
            if not isinstance(raw, dict):
                raw = {}
            # Merge common confirm shapes (nested raw vs flat confirm dict).
            blob: dict[str, Any] = {}
            if isinstance(close_data, dict):
                nested = close_data.get("confirm")
                if isinstance(nested, dict):
                    blob.update(nested)
                    nested_raw = nested.get("raw")
                    if isinstance(nested_raw, dict):
                        blob.update(nested_raw)
                blob.update(
                    {
                        k: close_data.get(k)
                        for k in ("profit", "level", "direction", "dealId")
                        if close_data.get(k) is not None
                    }
                )
            blob.update(conf)
            blob.update(raw)

            profit = None
            for key in ("profit", "pnl", "realized_pnl", "profitLoss"):
                if blob.get(key) is not None:
                    try:
                        profit = float(blob.get(key))
                        break
                    except (TypeError, ValueError):
                        pass
            if profit is None and pnl_gbp is not None:
                try:
                    profit = float(pnl_gbp)
                except (TypeError, ValueError):
                    profit = None
            # Still journal with 0.0 when broker omit profit — DealID round-trip proof.
            if profit is None:
                profit = 0.0

            exit_lvl = None
            for key in ("level", "exit", "exit_price", "price"):
                if blob.get(key) is not None:
                    try:
                        exit_lvl = float(blob.get(key))
                        break
                    except (TypeError, ValueError):
                        pass
            # Leave engine_origin empty so lane/env resolves MACRO_SENTINEL /
            # QUANT_SNIPER — do not overwrite with close-source tags like
            # "dynamic_limit" (breaks soak style/lane inference).
            reason_s = (
                f"{source}:{reason}"[:160] if reason else str(source or "")
            )
            style_hint = _style_hint
            reason_l = reason_s.lower()
            if style_hint is None and (
                "long_runner" in reason_l or "long_trade" in reason_l
            ):
                style_hint = "long"
            hold_hint = _hold_sec_hint
            if hold_hint is None:
                try:
                    from runtime.micro_gbp_exit import hold_sec_for_deal

                    hold_hint = hold_sec_for_deal(did)
                except Exception:
                    hold_hint = None
            record_trade_close(
                deal_id=did,
                direction=direction_u,
                entry_price=None,
                exit_price=exit_lvl,
                realized_pnl_gbp=float(profit),
                account_id=str(getattr(rest, "account_id", "") or ""),
                product_type="",
                engine_origin="",
                exit_reason=reason_s,
                hold_sec=hold_hint,
                style=style_hint,
                epic=epic_s,
            )
            log_engine(
                f"ExitGate: journal recorded deal={did[:12]} pnl={float(profit):.2f}"
            )
        except Exception as journal_exc:
            log_engine(
                f"ExitGate: journal record skipped deal={did[:12]} "
                f"{type(journal_exc).__name__}: {journal_exc}"
            )
        return result
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        result["error"] = err
        _record_flatten_failure(did, detail=err)
        log_engine(f"ExitGate: flatten FAILED deal={did[:12]} — {err}")
        epic_s = str(result.get("epic") or epic or "")
        try:
            from execution.instrument_suspension import (
                bind_rest_client,
                is_instrument_restriction,
                mark_deal_suspended,
            )
            from ig_api.exceptions import InstrumentSuspendedException

            if isinstance(exc, InstrumentSuspendedException) or is_instrument_restriction(
                exc
            ):
                bind_rest_client(rest)
                mark_deal_suspended(
                    did,
                    epic=epic_s,
                    status=getattr(exc, "status", None) or "EDITS_ONLY",
                    detail=err,
                    direction=str(direction or "BUY"),
                    size=float(size or 0),
                )
                result["suspended"] = True
                result["mode"] = "SUSPENDED"
        except Exception:
            pass
        try:
            from execution.edits_only_close_queue import enqueue_close

            enqueue_close(
                deal_id=did,
                epic=epic_s,
                direction=str(direction or "BUY"),
                size=float(size or 0),
                reason=str(reason or "")[:160],
                error=err,
                pnl_gbp=pnl_gbp,
            )
            result["queued"] = True
        except Exception:
            pass
        # Keep paused briefly so Micro cannot race; then allow retry via queue/OPM.
        # Never busy-wait on EDITS_ONLY — recovery poll owns the retry cadence.
        time.sleep(0.05)
        _resume_trackers_on_failure(did)
        return result
    finally:
        with _lock:
            _executing.discard(did)
            _last_result = dict(result)


def request_flatten_from_action(
    rest: Any,
    act: Any,
    *,
    cfg: Any | None = None,
    book: dict[str, dict[str, Any]] | None = None,
    source: str = "opm",
) -> dict[str, Any]:
    """Adapt ManageAction → request_flatten."""
    did = str(getattr(act, "deal_id", "") or "")
    epic = str(getattr(act, "epic", "") or "")
    direction = "BUY"
    size = 0.0
    if book and did in book:
        pos = (book[did].get("position") or {}) if isinstance(book[did], dict) else {}
        direction = str(pos.get("direction") or "BUY").upper()
        size = float(pos.get("size") or 0)
        mkt = book[did].get("market") or {}
        if mkt.get("epic"):
            epic = str(mkt.get("epic"))
    return request_flatten(
        rest=rest,
        deal_id=did,
        epic=epic,
        direction=direction,
        size=size,
        reason=str(getattr(act, "reason", "") or ""),
        pnl_gbp=getattr(act, "pnl_gbp", None),
        cfg=cfg,
        source=source,
    )
