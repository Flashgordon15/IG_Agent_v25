"""Per-account in-memory + cross-process order mutex — blocks concurrent entry storms.

Thread-safe compare-and-set: two threads cannot both observe unlocked and both
acquire. Caps / locks are **account-scoped** so CFD (Z6BAH4) and SB (Z6BAH3)
never block each other.

Hard-capped accounts (Z6BAH4→1) also use a disk ledger + flock so process
restarts and TWAP multi-clip paths cannot undercount opens.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from system.engine_log import log_engine

MUTEX_REJECT_LOG = (
    "[🛡️ RISK CIRCUIT BREAKER - ORDER REJECTED: MUTEX POSITION LOCK ACTIVE]"
)
HARD_CAP_REJECT_LOG = (
    "[🛡️ RISK CIRCUIT BREAKER - ORDER REJECTED: ACCOUNT HARD CAP ACTIVE]"
)
AMBIGUOUS_ORDER_TIMEOUT_SEC = 5.0

# Un-bypassable concurrent-open ceilings (runtime, independent of soft config).
# CFD sniper + SB sentinel both hard-capped at 1 — forbids same-second BUY+SELL
# opposite opens (DIAAAAX6AMAM3AH / DIAAAAX6AL9NQBD class).
HARD_OPEN_CAP_BY_ACCOUNT: dict[str, int] = {
    "Z6BAH4": 1,
    "Z6BAH3": 1,
}

# Process-local open ledger — bridges the REST/snapshot lag between fill and SoT.
# Incremented on dispatch confirm / slot reserve; decremented on known flatten.
_ledger_lock = threading.Lock()
_open_ledger: dict[str, int] = {}
# Slot reserved at signal-accept / mutex acquire (counts toward hard cap).
_reserved_slots: dict[str, int] = {}
# Alt-1 entry quarantine: after any hard-cap fill, block new entries until raw flat.
_entry_quarantine: dict[str, float] = {}


def _norm_account(account_id: str | None) -> str:
    raw = str(account_id or "").strip().upper()
    if raw:
        return raw
    return str(os.environ.get("IG_ACCOUNT_ID") or "").strip().upper()


def resolve_account_hard_open_cap(account_id: str | None = None) -> int | None:
    """Return un-bypassable max opens for account, or None when not hard-capped."""
    acct = _norm_account(account_id)
    if not acct:
        return None
    if acct in HARD_OPEN_CAP_BY_ACCOUNT:
        return int(HARD_OPEN_CAP_BY_ACCOUNT[acct])
    return None


def _disk_ledger_path(account_id: str) -> Path:
    from system.paths import data_dir

    acct = _norm_account(account_id) or "DEFAULT"
    # Per-lane isolation: CFD under state_cfd, SB under state_sb.
    if acct == "Z6BAH4":
        base = Path(data_dir()) / "state_cfd"
    elif acct == "Z6BAH3":
        base = Path(data_dir()) / "state_sb"
    else:
        base = Path(data_dir()) / "state"
    return base / f"hard_cap_ledger_{acct.lower()}.json"


def _flock_path(account_id: str) -> Path:
    from system.paths import data_dir

    acct = _norm_account(account_id) or "DEFAULT"
    if acct == "Z6BAH4":
        base = Path(data_dir()) / "state_cfd"
        return base / "z6bah4_hard_cap.lock"
    if acct == "Z6BAH3":
        base = Path(data_dir()) / "state_sb"
        return base / "z6bah3_hard_cap.lock"
    base = Path(data_dir()) / "state"
    return base / f"hard_cap_{acct.lower()}.lock"


def _read_disk_ledger(account_id: str) -> dict[str, Any]:
    path = _disk_ledger_path(account_id)
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {"open": 0, "reserved": 0, "updated_ts": 0.0}


def _write_disk_ledger(account_id: str, *, open_n: int, reserved_n: int) -> None:
    path = _disk_ledger_path(account_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "account_id": _norm_account(account_id),
            "open": max(0, int(open_n)),
            "reserved": max(0, int(reserved_n)),
            "updated_ts": time.time(),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log_engine(
            f"OrderMutex: disk ledger write failed account={account_id} "
            f"{type(exc).__name__}: {exc}"
        )


def disk_open_count(account_id: str | None = None) -> int:
    acct = _norm_account(account_id) or "DEFAULT"
    raw = _read_disk_ledger(acct)
    return max(0, int(raw.get("open") or 0) + int(raw.get("reserved") or 0))


def note_account_open(account_id: str | None = None, *, delta: int = 1) -> int:
    acct = _norm_account(account_id) or "DEFAULT"
    with _ledger_lock:
        cur = int(_open_ledger.get(acct, 0) or 0) + int(delta)
        _open_ledger[acct] = max(0, cur)
        mem = _open_ledger[acct]
        # Persist for hard-capped accounts (cross-process).
        if resolve_account_hard_open_cap(acct) is not None:
            disk = _read_disk_ledger(acct)
            disk_open = max(0, int(disk.get("open") or 0) + int(delta))
            _write_disk_ledger(
                acct,
                open_n=disk_open,
                reserved_n=int(disk.get("reserved") or 0),
            )
        return mem


def note_account_flat(account_id: str | None = None) -> None:
    acct = _norm_account(account_id) or "DEFAULT"
    with _ledger_lock:
        _open_ledger[acct] = 0
        _reserved_slots[acct] = 0
        _entry_quarantine.pop(acct, None)
        if resolve_account_hard_open_cap(acct) is not None:
            _write_disk_ledger(acct, open_n=0, reserved_n=0)


def arm_entry_quarantine(account_id: str | None = None) -> None:
    """After a hard-cap fill: refuse new entries until raw broker proves flat."""
    acct = _norm_account(account_id) or "DEFAULT"
    if resolve_account_hard_open_cap(acct) is None:
        return
    with _ledger_lock:
        _entry_quarantine[acct] = time.time()


def entry_quarantine_active(account_id: str | None = None) -> bool:
    acct = _norm_account(account_id) or "DEFAULT"
    with _ledger_lock:
        return acct in _entry_quarantine


def clear_entry_quarantine(account_id: str | None = None) -> None:
    acct = _norm_account(account_id) or "DEFAULT"
    with _ledger_lock:
        _entry_quarantine.pop(acct, None)


def memory_open_count(account_id: str | None = None) -> int:
    acct = _norm_account(account_id) or "DEFAULT"
    with _ledger_lock:
        return int(_open_ledger.get(acct, 0) or 0)


def reserved_slot_count(account_id: str | None = None) -> int:
    acct = _norm_account(account_id) or "DEFAULT"
    with _ledger_lock:
        mem_r = int(_reserved_slots.get(acct, 0) or 0)
    if resolve_account_hard_open_cap(acct) is not None:
        disk = _read_disk_ledger(acct)
        return max(mem_r, int(disk.get("reserved") or 0))
    return mem_r


def raw_broker_open_count(rest: Any | None) -> int | None:
    """Live GET /positions count for hard-cap — never coalesce to stale snapshot.

    Returns None when live count is unavailable. Callers must NOT treat None as
    flat (stale-zero clear re-armed Z6BAH4 cascades).
    """
    if rest is None:
        return None

    def _as_int(val: Any) -> int | None:
        try:
            if val is None:
                return None
            if type(val).__name__ == "MagicMock":
                return None
            return int(val)
        except (TypeError, ValueError):
            return None

    # Live-only for real clients. On failure return None — do NOT fall back to
    # open_positions()/count_open_positions() which may serve a stale-zero snapshot
    # under REST pressure and clear the hard-cap ledger.
    live_fn = getattr(rest, "count_open_positions_live", None)
    if callable(live_fn):
        try:
            return _as_int(live_fn())
        except Exception:
            return None
    # Unit-test doubles without live helper.
    try:
        if hasattr(rest, "count_open_positions"):
            return _as_int(rest.count_open_positions())
    except Exception:
        return None
    return None


def broker_open_count_authoritative(
    account_id: str | None = None,
    *,
    rest: Any | None = None,
) -> int | None:
    """Prefer raw live broker count; fall back to snapshot / trade_support SoT."""
    raw = raw_broker_open_count(rest)
    if raw is not None:
        return int(raw)
    _ = account_id  # snapshot is process-scoped to the active engine account
    try:
        from runtime.broker_snapshot import open_count_from_snapshot

        # Short TTL — stale undercount was a cascade vector.
        snap_n = open_count_from_snapshot(max_age_sec=15.0)
        if snap_n is not None:
            return int(snap_n)
    except Exception:
        pass
    try:
        from pathlib import Path
        import json as _json

        from system.paths import data_dir

        path = Path(data_dir()) / "trade_support_status.json"
        if path.is_file():
            raw_doc = _json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw_doc, dict) and raw_doc.get("broker_open") is not None:
                return int(raw_doc.get("broker_open") or 0)
    except Exception:
        pass
    return None


def effective_open_pressure(
    account_id: str | None = None,
    *,
    open_count: int | None = None,
    rest: Any | None = None,
) -> int:
    """max(broker, memory open, reserved, disk) — never undercount for hard cap."""
    mem_n = memory_open_count(account_id)
    res_n = reserved_slot_count(account_id)
    disk_n = (
        disk_open_count(account_id)
        if resolve_account_hard_open_cap(account_id) is not None
        else 0
    )
    if open_count is not None:
        broker_n = int(open_count)
    else:
        got = broker_open_count_authoritative(account_id, rest=rest)
        broker_n = int(got) if got is not None else 0
    return max(mem_n, res_n, disk_n, broker_n)


def sync_hard_cap_ledger_with_broker(
    account_id: str | None = None,
    *,
    rest: Any | None = None,
    force_broker_n: int | None = None,
) -> int | None:
    """Align memory/disk ledger to live broker opens for hard-capped accounts.

    Flat book + no in-flight → clear reservation so the next single entry can arm.
    Live opens → hydrate ledger so stale-zero snapshots cannot reopen a second ticket.

    CRITICAL: only clear on a successful live count of 0. A failed/None live
    fetch must leave the ledger intact (stale-zero clear re-armed cascades).
    """
    acct = _norm_account(account_id)
    if resolve_account_hard_open_cap(acct) is None:
        return None
    raw = int(force_broker_n) if force_broker_n is not None else raw_broker_open_count(rest)
    if raw is None:
        # Unknown book — keep existing pressure; do not undercount-clear.
        return None
    if get_order_mutex().is_locked(acct):
        # Never clear while a submit is in flight.
        if int(raw) > memory_open_count(acct):
            note_account_open(acct, delta=int(raw) - memory_open_count(acct))
        return int(raw)
    if int(raw) <= 0:
        note_account_flat(acct)
        return 0
    mem = memory_open_count(acct)
    if int(raw) > mem:
        note_account_open(acct, delta=int(raw) - mem)
    # Live open still present — keep quarantine armed.
    arm_entry_quarantine(acct)
    return int(raw)


def hard_cap_blocks_entry(
    account_id: str | None = None,
    *,
    open_count: int | None = None,
    rest: Any | None = None,
) -> tuple[bool, str]:
    """
    Hard-veto new allocations when opens/reserves >= account hard cap.

    Prefer reject when: order_in_flight OR open_count>=cap OR reserved_slot.
    Uses max(broker SoT/raw, in-memory fill ledger, disk ledger, reserved).
    """
    cap = resolve_account_hard_open_cap(account_id)
    if cap is None:
        return False, ""
    acct = _norm_account(account_id) or "UNKNOWN"

    # Note: order_in_flight is enforced by try_acquire / pre_submit_hard_cap_gate,
    # not here — callers check hard_cap then CAS-acquire (reject reason = mutex).

    # Live sync when rest provided (clears ledger after confirmed flatten).
    if rest is not None and open_count is None:
        try:
            sync_hard_cap_ledger_with_broker(account_id, rest=rest)
        except Exception:
            pass

    n = effective_open_pressure(account_id, open_count=open_count, rest=rest)
    # Hydrate memory ledger from live SoT only (not from explicit open_count probes).
    if open_count is None:
        broker_n = broker_open_count_authoritative(account_id, rest=rest)
        mem_n = memory_open_count(account_id)
        if broker_n is not None and int(broker_n) > mem_n:
            try:
                note_account_open(account_id, delta=int(broker_n) - mem_n)
            except Exception:
                pass

    if int(n) >= int(cap):
        acct = _norm_account(account_id) or "UNKNOWN"
        reason = (
            f"account_hard_cap:{acct} broker_open={int(n)} >= {int(cap)} "
            f"(flat book required)"
        )
        return True, reason
    return False, ""


@contextmanager
def hard_cap_flock(account_id: str | None = None) -> Iterator[Any]:
    """Cross-process exclusive flock for hard-capped account submit serialization."""
    acct = _norm_account(account_id)
    if resolve_account_hard_open_cap(acct) is None:
        yield None
        return
    import fcntl

    path = _flock_path(acct)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield fh
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass


def pre_submit_hard_cap_gate(
    account_id: str | None,
    *,
    rest: Any | None = None,
    source: str = "",
    mux_already_held: bool = False,
) -> tuple[bool, str, bool]:
    """
    Broker-authoritative last-line gate before ANY POST /positions/otc entry.

    Returns (allowed, reason, ledger_reserved).
    Always queries raw broker opens under flock when rest is provided — even if
    the in-process mutex is already held (TWAP clip-2+ must not bypass).

    When live raw==0 and mutex is free, stale mem/disk ledgers are cleared
    inside the flock (sync-alone can miss when an earlier live GET failed).
    """
    acct = _norm_account(account_id)
    cap = resolve_account_hard_open_cap(acct)
    if cap is None:
        return True, "", False

    # Live sync before gate so confirmed flats can re-arm a single slot.
    try:
        sync_hard_cap_ledger_with_broker(acct, rest=rest)
    except Exception:
        pass

    with hard_cap_flock(acct):
        # Prefer: in-flight / reserved / open>=cap → hard reject.
        if not mux_already_held and get_order_mutex().is_locked(acct):
            reason = (
                f"account_hard_cap:{acct} order_in_flight=1 "
                f"source={source or 'pre_submit'}"
            )
            log_engine(f"{HARD_CAP_REJECT_LOG} {reason}")
            return False, reason, False

        raw_n = raw_broker_open_count(rest)

        # Trust a successful live flat: clear stale mem/disk that blocked soak
        # after ambiguous-timeout releases left ledger=1 while broker_raw=0.
        if (
            raw_n is not None
            and int(raw_n) <= 0
            and not get_order_mutex().is_locked(acct)
        ):
            note_account_flat(acct)

        mem_n = memory_open_count(acct)
        res_n = reserved_slot_count(acct)
        disk_n = disk_open_count(acct)
        quarantined = entry_quarantine_active(acct)

        if mux_already_held:
            # Caller already reserved via try_acquire — do not self-block on
            # our own ledger slot. Veto when raw broker shows a live open OR
            # this mutex hold already posted one entry (TWAP / forceOpen stack).
            if raw_n is not None and int(raw_n) >= int(cap):
                reason = (
                    f"account_hard_cap:{acct} open={int(raw_n)} "
                    f"(broker_raw={raw_n}) >= {int(cap)} "
                    f"({source or 'pre_submit'} gate; mux held — stack blocked)"
                )
                log_engine(f"{HARD_CAP_REJECT_LOG} {reason}")
                return False, reason, False
            mux = get_order_mutex()
            with mux._lock:
                meta = mux._meta.get(acct) or {}
                if meta.get("entry_posted"):
                    reason = (
                        f"account_hard_cap:{acct} entry_posted=1 under mutex "
                        f"({source or 'pre_submit'} gate; second submit blocked)"
                    )
                    log_engine(f"{HARD_CAP_REJECT_LOG} {reason}")
                    return False, reason, False
                meta = dict(meta)
                meta["entry_posted"] = True
                mux._meta[acct] = meta
            return True, "", False

        # Fail-closed when live count unavailable AND pressure/quarantine present.
        if raw_n is None and (mem_n > 0 or res_n > 0 or disk_n > 0 or quarantined):
            reason = (
                f"account_hard_cap:{acct} live_count_unavailable "
                f"(mem={mem_n} reserved={res_n} disk={disk_n} "
                f"quarantine={int(quarantined)}) "
                f"({source or 'pre_submit'} gate; fail-closed)"
            )
            log_engine(f"{HARD_CAP_REJECT_LOG} {reason}")
            return False, reason, False

        if quarantined and (raw_n is None or int(raw_n) > 0):
            reason = (
                f"account_hard_cap:{acct} entry_quarantine_until_flat "
                f"broker_raw={raw_n if raw_n is not None else -1} "
                f"({source or 'pre_submit'} gate)"
            )
            log_engine(f"{HARD_CAP_REJECT_LOG} {reason}")
            return False, reason, False

        effective = max(
            mem_n,
            res_n,
            disk_n,
            int(raw_n) if raw_n is not None else 0,
        )
        if effective >= int(cap):
            reason = (
                f"account_hard_cap:{acct} open={effective} "
                f"(mem={mem_n} reserved={res_n} disk={disk_n} "
                f"broker_raw={raw_n if raw_n is not None else -1}) "
                f">= {int(cap)} ({source or 'pre_submit'} gate)"
            )
            log_engine(f"{HARD_CAP_REJECT_LOG} {reason}")
            return False, reason, False

        # Reserve slot now (signal/submit accept) so parallel waiters see pressure.
        with _ledger_lock:
            _open_ledger[acct] = int(_open_ledger.get(acct, 0) or 0) + 1
            _reserved_slots[acct] = int(_reserved_slots.get(acct, 0) or 0) + 1
        disk = _read_disk_ledger(acct)
        _write_disk_ledger(
            acct,
            open_n=max(int(disk.get("open") or 0), mem_n + 1),
            reserved_n=max(int(disk.get("reserved") or 0), res_n + 1),
        )
        return True, "", True


def release_pre_submit_reservation(
    account_id: str | None,
    *,
    filled: bool,
) -> None:
    """Roll back pre-submit ledger reservation on reject; keep on fill."""
    acct = _norm_account(account_id) or "DEFAULT"
    if resolve_account_hard_open_cap(acct) is None:
        return
    with _ledger_lock:
        if filled:
            # Convert reserved → confirmed open.
            res = int(_reserved_slots.get(acct, 0) or 0)
            if res > 0:
                _reserved_slots[acct] = res - 1
            # open ledger already incremented at reserve.
        else:
            cur = int(_open_ledger.get(acct, 0) or 0)
            _open_ledger[acct] = max(0, cur - 1)
            res = int(_reserved_slots.get(acct, 0) or 0)
            _reserved_slots[acct] = max(0, res - 1)
    disk = _read_disk_ledger(acct)
    if filled:
        _write_disk_ledger(
            acct,
            open_n=max(1, int(disk.get("open") or 0)),
            reserved_n=max(0, int(disk.get("reserved") or 0) - 1),
        )
        arm_entry_quarantine(acct)
    else:
        _write_disk_ledger(
            acct,
            open_n=max(0, int(disk.get("open") or 0) - 1),
            reserved_n=max(0, int(disk.get("reserved") or 0) - 1),
        )


class AccountOrderMutex:
    """Process-global per-account ``order_in_flight`` gate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # account_id -> acquired monotonic timestamp
        self._in_flight: dict[str, float] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        # Router-facing mirror: True when *any* account slot is held.
        self.order_in_flight: bool = False

    def is_locked(self, account_id: str | None = None) -> bool:
        acct = _norm_account(account_id)
        with self._lock:
            if not acct:
                return bool(self._in_flight)
            return acct in self._in_flight

    def age_sec(self, account_id: str | None = None) -> float | None:
        acct = _norm_account(account_id)
        with self._lock:
            ts = self._in_flight.get(acct)
        if ts is None:
            return None
        return max(0.0, time.monotonic() - float(ts))

    def try_acquire(
        self,
        account_id: str | None = None,
        *,
        epic: str = "",
        source: str = "",
    ) -> bool:
        """Atomic acquire — returns False if already in flight for this account.

        For hard-capped accounts (Z6BAH4→1), also reserves a memory+disk ledger
        slot under the same lock so concurrent waiters cannot all pass a
        pre-mutex ``hard_cap_blocks_entry`` check at open_count=0.
        """
        acct = _norm_account(account_id)
        if not acct:
            acct = "DEFAULT"
        with self._lock:
            if acct in self._in_flight:
                return False
            reserved = False
            cap = HARD_OPEN_CAP_BY_ACCOUNT.get(acct)
            if cap is not None:
                with _ledger_lock:
                    mem = int(_open_ledger.get(acct, 0) or 0)
                    res = int(_reserved_slots.get(acct, 0) or 0)
                    disk = _read_disk_ledger(acct)
                    disk_pressure = max(
                        int(disk.get("open") or 0) + int(disk.get("reserved") or 0),
                        mem,
                        res,
                    )
                    if disk_pressure >= int(cap):
                        return False
                    _open_ledger[acct] = mem + 1
                    _reserved_slots[acct] = res + 1
                    reserved = True
                    _write_disk_ledger(
                        acct,
                        open_n=max(int(disk.get("open") or 0), mem + 1),
                        reserved_n=max(int(disk.get("reserved") or 0), res + 1),
                    )
            now = time.monotonic()
            self._in_flight[acct] = now
            self._meta[acct] = {
                "epic": str(epic or ""),
                "source": str(source or ""),
                "acquired_mono": now,
                "acquired_wall": time.time(),
                "ledger_reserved": reserved,
            }
            self.order_in_flight = True
            return True

    def release(
        self,
        account_id: str | None = None,
        *,
        reason: str = "broker_confirm",
        filled: bool | None = None,
    ) -> bool:
        """Release after authenticated broker fill/reject/confirm JSON.

        When ``filled`` is False, any hard-cap ledger reservation from
        ``try_acquire`` is rolled back. When ``filled`` is True, the
        reservation stands (counts as the open). When ``filled`` is None,
        reservation is left unchanged (legacy callers).
        """
        acct = _norm_account(account_id)
        if not acct:
            acct = "DEFAULT"
        with self._lock:
            existed = self._in_flight.pop(acct, None) is not None
            meta = self._meta.pop(acct, None) or {}
            self.order_in_flight = bool(self._in_flight)
        if existed and meta.get("ledger_reserved") and filled is False:
            with _ledger_lock:
                cur = int(_open_ledger.get(acct, 0) or 0)
                _open_ledger[acct] = max(0, cur - 1)
                res = int(_reserved_slots.get(acct, 0) or 0)
                _reserved_slots[acct] = max(0, res - 1)
            if resolve_account_hard_open_cap(acct) is not None:
                disk = _read_disk_ledger(acct)
                _write_disk_ledger(
                    acct,
                    open_n=max(0, int(disk.get("open") or 0) - 1),
                    reserved_n=max(0, int(disk.get("reserved") or 0) - 1),
                )
        elif existed and meta.get("ledger_reserved") and filled is True:
            with _ledger_lock:
                res = int(_reserved_slots.get(acct, 0) or 0)
                _reserved_slots[acct] = max(0, res - 1)
            if resolve_account_hard_open_cap(acct) is not None:
                disk = _read_disk_ledger(acct)
                _write_disk_ledger(
                    acct,
                    open_n=max(1, int(disk.get("open") or 0)),
                    reserved_n=max(0, int(disk.get("reserved") or 0) - 1),
                )
                arm_entry_quarantine(acct)
        if existed:
            log_engine(
                f"OrderMutex: released account={acct} reason={reason}"
            )
        return existed

    def force_clear(
        self,
        account_id: str | None = None,
        *,
        reason: str = "orchestrator_ambiguous_timeout",
    ) -> bool:
        """Emergency clear — used by ambiguous-order reconciler."""
        return self.release(account_id, reason=reason)

    def ambiguous_accounts(
        self, *, timeout_sec: float = AMBIGUOUS_ORDER_TIMEOUT_SEC
    ) -> list[str]:
        limit = max(0.1, float(timeout_sec))
        now = time.monotonic()
        aged: list[str] = []
        with self._lock:
            for acct, ts in self._in_flight.items():
                if (now - float(ts)) > limit:
                    aged.append(acct)
        return aged

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            accounts = {
                acct: {
                    "order_in_flight": True,
                    "age_sec": round(now - float(ts), 3),
                    **(self._meta.get(acct) or {}),
                }
                for acct, ts in self._in_flight.items()
            }
            any_locked = bool(self._in_flight)
            mirror = self.order_in_flight
        return {
            "order_in_flight": bool(any_locked or mirror),
            "accounts": accounts,
            "ambiguous_timeout_sec": AMBIGUOUS_ORDER_TIMEOUT_SEC,
            "hard_open_caps": dict(HARD_OPEN_CAP_BY_ACCOUNT),
        }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._in_flight.clear()
            self._meta.clear()
            self.order_in_flight = False
        with _ledger_lock:
            _open_ledger.clear()
            _reserved_slots.clear()
            _entry_quarantine.clear()
        for acct in list(HARD_OPEN_CAP_BY_ACCOUNT.keys()):
            try:
                _write_disk_ledger(acct, open_n=0, reserved_n=0)
            except Exception:
                pass


_MUTEX = AccountOrderMutex()


def get_order_mutex() -> AccountOrderMutex:
    return _MUTEX


def try_acquire_order_mutex(
    account_id: str | None = None,
    *,
    epic: str = "",
    source: str = "",
) -> bool:
    return _MUTEX.try_acquire(account_id, epic=epic, source=source)


def release_order_mutex(
    account_id: str | None = None,
    *,
    reason: str = "broker_confirm",
    filled: bool | None = None,
) -> bool:
    return _MUTEX.release(account_id, reason=reason, filled=filled)


def log_mutex_reject(*, account_id: str | None = None, source: str = "") -> None:
    acct = _norm_account(account_id) or "UNKNOWN"
    suffix = f" account={acct}"
    if source:
        suffix += f" source={source}"
    log_engine(f"{MUTEX_REJECT_LOG}{suffix}")


def mutex_veto_payload(
    *,
    account_id: str | None = None,
    source: str = "",
) -> dict[str, Any]:
    log_mutex_reject(account_id=account_id, source=source)
    return {
        "vetoed": True,
        "status": "REJECTED",
        "reason": "mutex_position_lock_active",
        "rejection_reason": MUTEX_REJECT_LOG,
        "dealReference": None,
        "orderType": "MARKET",
        "mutex_lock": True,
        "account_id": _norm_account(account_id),
        "source": source,
    }


def reset_order_mutex_for_tests() -> None:
    _MUTEX.reset_for_tests()
