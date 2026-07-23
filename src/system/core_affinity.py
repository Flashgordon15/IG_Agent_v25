"""
Core separation model — CFD sniper vs SB sentinel (v33/v34).

Thread / core model (documentation + optional affinity hints):
  - **Core 1 — QUANT SNIPER (Z6BAH4 / CFD :8080)**
    Uncapped open positions (``engine_position_caps`` null for CFD lane).
    Hot sweep + SHM writers for soft_loss / trail / ATR limits.
  - **Core 2 — MACRO SENTINEL (Z6BAH3 / SB :8081)**
    Spreadbet cap 10 opens. Independent REST token bucket (10 req/s).

``os.sched_setaffinity`` is guarded — macOS lacks ``sched_setaffinity`` entirely;
Linux twin engines pin to physical cores 1/2 when available. Failures are
swallowed so desk boot is never blocked.

Jitter intent (v34): sub-5ms scheduling variance on pinned hot paths — a design
target for colocated sweep threads, **not** a runtime guarantee.

Independent worker threads:
  - ``performance-journal`` daemon (cold CSV)
  - ``dashboard-api`` thread pool (FastAPI sync lanes)
  - Async execution tasks should prefer ``threading.Thread(..., name=...)`` with
    ``hint_core_for_thread`` at spawn sites (non-breaking).
"""

from __future__ import annotations

import os
import threading
from typing import Any

_ENGINE_CORE_HINT: dict[str, int] = {
    "QUANT_SNIPER": 1,
    "MACRO_SENTINEL": 2,
    "cfd_sniper": 1,
    "sb_sentinel": 2,
}


def resolve_engine_core(engine_origin: str | None = None) -> int | None:
    key = str(engine_origin or os.environ.get("IG_ENGINE_ORIGIN") or "").strip()
    if not key:
        try:
            from system.engine_lane import infer_engine_id

            key = infer_engine_id()
        except Exception:
            return None
    low = key.lower()
    for token, core in _ENGINE_CORE_HINT.items():
        if token.lower() in low or low in token.lower():
            return core
    return None


def _apply_sched_affinity(pid_or_tid: int, core: int) -> bool:
    try:
        if not hasattr(os, "sched_setaffinity"):
            return False
        os.sched_setaffinity(int(pid_or_tid), {int(core)})  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def hint_core_for_thread(
    thread: threading.Thread | None = None,
    *,
    engine_origin: str | None = None,
    core: int | None = None,
) -> bool:
    """Best-effort affinity — returns True when sched_setaffinity succeeded."""
    target_core = core if core is not None else resolve_engine_core(engine_origin)
    if target_core is None:
        return False
    tid = None
    if thread is not None and thread.ident is not None:
        tid = thread.ident
    else:
        tid = threading.get_ident()
    return _apply_sched_affinity(tid, int(target_core))


def pin_current_process_to_engine(
    engine_origin: str | None = None,
    *,
    core: int | None = None,
) -> dict[str, Any]:
    """
    Pin the current process to QUANT_SNIPER→core 1 or MACRO_SENTINEL→core 2.

    macOS: ``sched_setaffinity`` is absent — logs once and returns ``pinned=False``
    without raising. Linux: best-effort physical core pin for <5ms jitter intent.
    """
    target_core = core if core is not None else resolve_engine_core(engine_origin)
    origin = str(
        engine_origin or os.environ.get("IG_ENGINE_ORIGIN") or ""
    ).strip()
    if target_core is None:
        return {
            "pinned": False,
            "core": None,
            "origin": origin or None,
            "reason": "core_unresolved",
        }

    if not hasattr(os, "sched_setaffinity"):
        try:
            from system.engine_log import log_engine

            log_engine(
                f"core_affinity: sched_setaffinity unavailable on {os.uname().sysname} "
                f"— no-op for origin={origin or '-'} target_core={target_core} "
                "(jitter target <5ms intent only)"
            )
        except Exception:
            pass
        return {
            "pinned": False,
            "core": int(target_core),
            "origin": origin or None,
            "reason": "sched_setaffinity_unavailable",
        }

    ok = _apply_sched_affinity(os.getpid(), int(target_core))
    try:
        from system.engine_log import log_engine

        if ok:
            log_engine(
                f"core_affinity: pinned pid={os.getpid()} origin={origin or '-'} "
                f"→ core {target_core} (jitter target <5ms intent, not guaranteed)"
            )
        else:
            log_engine(
                f"core_affinity: pin failed pid={os.getpid()} origin={origin or '-'} "
                f"target_core={target_core}"
            )
    except Exception:
        pass
    return {
        "pinned": ok,
        "core": int(target_core),
        "origin": origin or None,
        "reason": "ok" if ok else "sched_setaffinity_rejected",
    }


def core_model_doc() -> dict[str, Any]:
    return {
        "core1": {
            "engine": "QUANT_SNIPER",
            "account": "Z6BAH4",
            "product": "CFD",
            "position_cap": None,
            "token_bucket_rps": 40,
        },
        "core2": {
            "engine": "MACRO_SENTINEL",
            "account": "Z6BAH3",
            "product": "SPREADBET",
            "position_cap": 10,
            "token_bucket_rps": 10,
        },
        "affinity": "best-effort sched_setaffinity (macOS may no-op)",
    }
