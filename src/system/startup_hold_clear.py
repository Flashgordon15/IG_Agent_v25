"""Clear stale entry/deploy holds when the broker book is flat at startup.

Cap-breach and stability-harness pauses are safety holds — they must not
survive a flat-book cold start or twin relaunch or entries stay blocked
while iron_cage reports trade_ready.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from system.paths import data_dir

_STALE_REASON_MARKERS = (
    "cap_breach",
    "stability_harness",
    "orchestrator:port_offline",
    "orchestrator:engine_drop",
)

_HOLD_FILES = ("entry_halt.json", "trading_paused.json", "offline_for_dev.json")


def _state_roots() -> list[Path]:
    root = Path(data_dir())
    candidates = (
        root / "state",
        root / "state_cfd",
        root / "state_sb",
    )
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _read_flag(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _is_stale_active_hold(raw: dict[str, Any]) -> bool:
    if not bool(raw.get("active")):
        return False
    reason = str(raw.get("reason") or "").lower()
    return any(marker in reason for marker in _STALE_REASON_MARKERS)


def book_flat_via_api(port: int = 8080, *, timeout: float = 2.0) -> bool | None:
    """True=flat, False=open risk, None=API unreachable.

    Never trust GUI ``count``/``verdict`` alone — ``broker_open_sot`` and
    trade_support can show real opens while row cache is empty (false FLAT).
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/api/positions/live",
            timeout=max(0.5, float(timeout)),
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not isinstance(body, dict):
            return None
        verdict = str(body.get("verdict") or "").upper()
        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        sot = body.get("broker_open_sot") if isinstance(body.get("broker_open_sot"), dict) else {}
        ts = body.get("trade_support") if isinstance(body.get("trade_support"), dict) else {}
        try:
            sot_count = int(sot.get("count") or 0)
        except (TypeError, ValueError):
            sot_count = 0
        try:
            ts_open = int(ts.get("broker_open") or 0)
        except (TypeError, ValueError):
            ts_open = 0
        open_risk = max(count, sot_count, ts_open)
        if open_risk > 0:
            return False
        if verdict == "FLAT" and count <= 0 and sot_count <= 0 and ts_open <= 0:
            return True
        if count > 0 or verdict not in ("FLAT", "", "HEALTHY"):
            return False
        return True
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None


def clear_stale_entry_holds_if_flat(
    *,
    port: int = 8080,
    reason: str = "startup_hold_clear",
    allow_offline_stale_clear: bool = True,
) -> dict[str, Any]:
    """
    Clear cap-breach / harness / orchestrator port_offline holds when safe.

    When the positions API is reachable and the book is not flat, no-op.
    When the API is down (cold start), stale marker holds are still cleared
    if *allow_offline_stale_clear* is True.
    """
    flat = book_flat_via_api(port)
    if flat is False:
        return {"cleared": [], "skipped": "book_not_flat", "flat": False}

    cleared: list[str] = []
    payload = {
        "active": False,
        "reason": str(reason or "startup_hold_clear"),
        "ts": time.time(),
    }
    text = json.dumps(payload, indent=2)

    for state_root in _state_roots():
        state_root.mkdir(parents=True, exist_ok=True)
        for name in _HOLD_FILES:
            path = state_root / name
            raw = _read_flag(path)
            if not _is_stale_active_hold(raw):
                continue
            if flat is None and not allow_offline_stale_clear:
                continue
            path.write_text(text, encoding="utf-8")
            cleared.append(str(path.relative_to(Path(data_dir()))))

    deploy_cleared = False
    try:
        from runtime.deploy_hold import _read_hold_file, set_deploy_hold

        hold = _read_hold_file()
        hold_reason = str(hold.get("reason") or "").lower()
        if bool(hold.get("active")) and any(
            m in hold_reason for m in _STALE_REASON_MARKERS
        ):
            if flat is not False:
                set_deploy_hold(active=False, reason=f"{reason}:deploy_hold")
                deploy_cleared = True
    except Exception:
        pass

    return {
        "cleared": cleared,
        "deploy_hold_cleared": deploy_cleared,
        "flat": flat,
        "skipped": None if cleared or deploy_cleared else "no_stale_holds",
    }
