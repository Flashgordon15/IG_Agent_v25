"""Master strategy kill-switch — freeze entries and signal generation on broker drift."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

_KILL_REASON = "BROKER_STATE_MISMATCH"
_LOCK = threading.Lock()
_tripped = False
_KILL_FILE = data_dir() / "state" / "strategy_kill_switch.json"


def is_strategy_kill_active() -> bool:
    """Return True when a broker reconciliation kill-switch is engaged."""
    global _tripped
    with _LOCK:
        if _tripped:
            return True
    if not _KILL_FILE.is_file():
        return False
    try:
        raw = json.loads(_KILL_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("ts") or 0)
        if age < 0 or age > 86400.0:
            return False
        with _LOCK:
            _tripped = True
        return True
    except Exception:
        return True


def trip_master_strategy_kill_switch(
    *,
    deal_id: str,
    reason: str,
    notify: bool = True,
) -> None:
    """
    Halt outbound trading loops and suppress new signal generation.

    Idempotent — only the first trip dispatches Telegram and loop blocks.
    """
    global _tripped
    with _LOCK:
        if _tripped:
            return
        _tripped = True

    try:
        _KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KILL_FILE.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "deal_id": str(deal_id or ""),
                    "reason": str(reason or _KILL_REASON),
                }
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        log_engine(f"strategy_kill_switch: persist failed: {type(exc).__name__}: {exc}")

    try:
        from system.qmm_process_supervisor import set_process_entry_block

        set_process_entry_block(_KILL_REASON)
    except Exception as exc:
        log_engine(f"strategy_kill_switch: process block failed: {type(exc).__name__}: {exc}")

    _apply_orchestrator_entry_blocks(active=True)

    log_engine(
        f"strategy_kill_switch: MASTER KILL engaged deal={deal_id} reason={reason}"
    )

    if notify:
        msg = (
            f"🚨 CRITICAL STATE MISMATCH: Position dealId={deal_id} was closed on the "
            f"broker. Strategy loop frozen for safety."
        )
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(msg, dedupe_key=f"broker_mismatch:{deal_id or 'unknown'}")
        except Exception as exc:
            log_engine(
                f"strategy_kill_switch: telegram failed: {type(exc).__name__}: {exc}"
            )


def clear_strategy_kill_switch_for_tests() -> None:
    clear_strategy_kill_switch()


def clear_strategy_kill_switch() -> None:
    """Clear master kill-switch latch and process entry block."""
    global _tripped
    with _LOCK:
        _tripped = False
    try:
        _KILL_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        from system.qmm_process_supervisor import clear_process_entry_block

        clear_process_entry_block()
    except Exception:
        pass
    _apply_orchestrator_entry_blocks(active=False)


def _apply_orchestrator_entry_blocks(*, active: bool) -> None:
    try:
        from runtime.market_orchestrator import MarketOrchestrator

        ref = getattr(MarketOrchestrator, "_ORCHESTRATOR_REF", None)
        if ref is None:
            from runtime import market_orchestrator as mo

            ref = mo._ORCHESTRATOR_REF
        if ref is None:
            return
        for loop in getattr(ref, "_loops", []) or []:
            if active:
                loop.set_entry_circuit_breaker(_KILL_REASON)
            else:
                loop.clear_entry_circuit_breaker()
    except Exception:
        pass


def kill_switch_snapshot() -> dict[str, Any]:
    if not _KILL_FILE.is_file():
        return {"active": False}
    try:
        raw = json.loads(_KILL_FILE.read_text(encoding="utf-8"))
        return {"active": is_strategy_kill_active(), **raw}
    except Exception:
        return {"active": is_strategy_kill_active()}
