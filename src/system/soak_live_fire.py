"""
15-minute soak live-fire injection — file-triggered WIN_ZONE stamps.

External runner (`scripts/autonomous_soak_test.py`) arms triggers every 3 minutes;
the Gold bare-metal tick consumes each trigger once and dispatches via LiveExecutor.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_SOAK_LOCK = threading.Lock()
_DATA = Path(__file__).resolve().parents[1] / "data"
SOAK_TRIGGER_PATH = _DATA / "soak_win_zone_trigger.json"
SOAK_RESULT_PATH = _DATA / "soak_win_zone_result.json"
SOAK_LEDGER_PATH = _DATA / "logs" / "soak_live_fire_ledger.jsonl"

SOAK_SIGNATURE = "SOAK_LIVE_FIRE"
GOLD_EPIC = "CS.D.CFPGOLD.CFP.IP"


def soak_mode_enabled() -> bool:
    return os.environ.get("IG_SOAK_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def arm_soak_injection(
    *,
    sequence: int,
    epic: str = GOLD_EPIC,
    action: str = "BUY",
    size: float = 0.1,
) -> None:
    """Arm the next bare-metal tick to stamp WIN_ZONE and dispatch a demo order."""
    payload = {
        "sequence": int(sequence),
        "epic": str(epic).strip(),
        "action": str(action).upper(),
        "size": float(size),
        "order_type": "MARKET",
        "signature": SOAK_SIGNATURE,
        "armed_at": time.time(),
        "consumed": False,
    }
    with _SOAK_LOCK:
        _write_json(SOAK_TRIGGER_PATH, payload)


def soak_armed_for_epic(epic: str) -> bool:
    """True when an external soak runner has armed WIN_ZONE for this epic."""
    if not soak_mode_enabled():
        return False
    with _SOAK_LOCK:
        state = _read_json(SOAK_TRIGGER_PATH)
        return bool(
            state
            and not state.get("consumed")
            and str(state.get("epic") or "").strip() == str(epic or "").strip()
        )


def try_consume_soak_injection(epic: str) -> dict[str, Any] | None:
    """Consume one armed soak trigger for the matching epic."""
    if not soak_mode_enabled():
        return None
    with _SOAK_LOCK:
        state = _read_json(SOAK_TRIGGER_PATH)
        if not state or state.get("consumed"):
            return None
        if str(state.get("epic") or "").strip() != str(epic or "").strip():
            return None
        state["consumed"] = True
        state["consumed_at"] = time.time()
        _write_json(SOAK_TRIGGER_PATH, state)
        return dict(state)


def record_soak_result(
    *,
    sequence: int,
    success: bool,
    deal_id: str,
    http_status: int,
) -> None:
    row = {
        "sequence": int(sequence),
        "success": bool(success),
        "deal_id": str(deal_id or ""),
        "http_status": int(http_status),
        "recorded_at": time.time(),
        "signature": SOAK_SIGNATURE,
    }
    with _SOAK_LOCK:
        _write_json(SOAK_RESULT_PATH, row)
        SOAK_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SOAK_LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


def read_soak_result() -> dict[str, Any]:
    return _read_json(SOAK_RESULT_PATH)


def clear_soak_artifacts() -> None:
    with _SOAK_LOCK:
        for path in (SOAK_TRIGGER_PATH, SOAK_RESULT_PATH):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def emit_soak_telemetry(
    *,
    epic: str,
    direction: str,
    entry: float,
    size: float,
    deal_id: str,
    coordinate: int,
    confidence: float,
    latency_us: float,
    success: bool,
    sequence: int,
) -> None:
    """SHM cockpit fill + fulfillment row + immediate Telegram."""
    status = "OPEN" if success else "REJECT"
    result = "SOAK" if success else "LOSS"

    try:
        from system.unified_fulfillment_cache import record_execution_performance_row

        record_execution_performance_row(
            epic=epic,
            direction=direction,
            result=result,
            confidence=confidence,
            cell_index=coordinate,
            latency_us=latency_us,
            deal_id=deal_id,
            size=size,
            entry=entry,
            exit=entry,
            pnl_gbp=0.0,
            status=status,
        )
    except Exception:
        pass

    try:
        from system.ipc.ring_buffer import publish_live_probe_cockpit

        publish_live_probe_cockpit(
            epic=epic,
            direction=direction,
            entry=entry,
            size=size,
            status=status,
            signature=f"{SOAK_SIGNATURE}#{sequence}",
        )
    except Exception:
        pass

    try:
        from system.unified_fulfillment_cache import get_fulfillment_payload
        from system.ipc.ring_buffer import publish_cockpit_shm

        publish_cockpit_shm(get_fulfillment_payload())
    except Exception:
        pass

    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier is None:
            return
        ok = "PLACED" if success else "REJECTED"
        text = (
            f"🔥 SOAK LIVE-FIRE [{SOAK_SIGNATURE} #{sequence}]\n"
            f"{epic} {direction} @ {entry:.2f} size={size:g}\n"
            f"deal={deal_id or '—'} status={ok} coord={coordinate}"
        )
        notifier.send_now(text)
    except Exception:
        pass
