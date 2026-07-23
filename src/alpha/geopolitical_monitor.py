"""Crude oil + VIX macro cooling monitor — async, off the tick lane.

Passively samples hub cache (WTI CFD) and light Yahoo proxy ticks for Brent
(``BZ=F``) and VIX (``^VIX``). When Brent < $84 and VIX < 16.5, emits:

    [MACRO_ALERT] Volatility premium cooling. Market normalizing.

State is written to disk so the Grok Oracle / operators can decide when it is
safe to lift ``grok_macro_bias`` from VETO → NEUTRAL. Zero work on the
Lightstreamer event lane — daemon poll only.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

BRENT_COOL_USD = 84.0
VIX_COOL = 16.5
POLL_SEC = 60.0
WTI_EPIC = "CS.D.CRUDE.CFD.IP"
YAHOO_SYMBOLS = ("CL=F", "BZ=F", "^VIX")

_ALERT_MSG = "[MACRO_ALERT] Volatility premium cooling. Market normalizing."

_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_started = False
_state: dict[str, Any] = {
    "brent": None,
    "wti": None,
    "vix": None,
    "volatility_premium_cooling": False,
    "safe_for_veto_lift": False,
    "last_alert_ts": None,
    "ts": 0.0,
    "source": "idle",
}
_last_alert_mono = 0.0
_ALERT_COOLDOWN_SEC = 300.0


def reset_geopolitical_monitor_for_tests() -> None:
    global _started, _thread, _last_alert_mono, _state
    stop_geopolitical_monitor()
    with _lock:
        _state = {
            "brent": None,
            "wti": None,
            "vix": None,
            "volatility_premium_cooling": False,
            "safe_for_veto_lift": False,
            "last_alert_ts": None,
            "ts": 0.0,
            "source": "idle",
        }
        _last_alert_mono = 0.0


def state_path() -> Path:
    """Production-only path — ``src/data/v31-production/state/`` (no legacy)."""
    from system.paths import v31_production_data_dir

    return v31_production_data_dir() / "state" / "geopolitical_macro.json"


def read_macro_cooling_state(*, max_age_sec: float | None = 600.0) -> dict[str, Any]:
    """Disk + memory snapshot for Grok / operators (never blocks, never raises)."""
    with _lock:
        mem = dict(_state)
    path = state_path()
    disk: dict[str, Any] | None = None
    try:
        if path.is_file():
            disk = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        disk = None
    if isinstance(disk, dict):
        age = time.time() - float(disk.get("ts") or 0)
        if max_age_sec is None or age <= float(max_age_sec):
            disk["age_sec"] = round(age, 1)
            return disk
    mem["age_sec"] = round(max(0.0, time.time() - float(mem.get("ts") or 0)), 1)
    return mem


def safe_for_veto_lift() -> bool:
    """True when oil/VIX cooling criteria are met — advisory only (does not flip bias)."""
    st = read_macro_cooling_state(max_age_sec=900.0)
    return bool(st.get("safe_for_veto_lift") or st.get("volatility_premium_cooling"))


def evaluate_macro_ticks(
    *,
    brent: float | None,
    wti: float | None,
    vix: float | None,
    source: str = "eval",
) -> dict[str, Any]:
    """Pure evaluation — unit-testable, no I/O."""
    brent_f = float(brent) if brent is not None and float(brent) > 0 else None
    wti_f = float(wti) if wti is not None and float(wti) > 0 else None
    vix_f = float(vix) if vix is not None and float(vix) > 0 else None
    # Prefer Brent; fall back to WTI proxy when Brent unavailable.
    oil = brent_f if brent_f is not None else wti_f
    cooling = (
        oil is not None
        and vix_f is not None
        and oil < BRENT_COOL_USD
        and vix_f < VIX_COOL
    )
    return {
        "brent": brent_f,
        "wti": wti_f,
        "vix": vix_f,
        "oil_ref": oil,
        "volatility_premium_cooling": cooling,
        "safe_for_veto_lift": cooling,
        "source": source,
        "thresholds": {"brent_usd": BRENT_COOL_USD, "vix": VIX_COOL},
    }


def _write_state(payload: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = dict(payload)
        body["ts"] = time.time()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _hub_wti_mid() -> float | None:
    try:
        from system.market_data_hub import get_market_data_hub

        q = get_market_data_hub().get_quote(WTI_EPIC)
        if q is None:
            return None
        mid = float(getattr(q, "mid", 0) or 0)
        return mid if mid > 0 else None
    except Exception:
        return None


def _yahoo_proxy_mids() -> dict[str, float]:
    """Light batch fetch — only from daemon thread, never tick lane."""
    try:
        from feeder.yahoo_quote_poller import fetch_yahoo_mids_batch

        return fetch_yahoo_mids_batch(list(YAHOO_SYMBOLS), token_wait_sec=0.05) or {}
    except Exception:
        return {}


def _apply_and_maybe_alert(eval_row: dict[str, Any]) -> dict[str, Any]:
    global _last_alert_mono
    payload = {
        **eval_row,
        "ts": time.time(),
        "last_alert_ts": _state.get("last_alert_ts"),
    }
    with _lock:
        _state.update(payload)
    _write_state(payload)
    if payload.get("volatility_premium_cooling"):
        now = time.monotonic()
        if now - _last_alert_mono >= _ALERT_COOLDOWN_SEC:
            _last_alert_mono = now
            payload["last_alert_ts"] = time.time()
            with _lock:
                _state["last_alert_ts"] = payload["last_alert_ts"]
            _write_state(payload)
            log_engine(_ALERT_MSG)
    return payload


def poll_once() -> dict[str, Any]:
    """One observation cycle — safe to call from tests or admin diagnostics."""
    wti = _hub_wti_mid()
    mids = _yahoo_proxy_mids()
    if wti is None and mids.get("CL=F"):
        wti = float(mids["CL=F"])
    brent = float(mids["BZ=F"]) if mids.get("BZ=F") else None
    vix = float(mids["^VIX"]) if mids.get("^VIX") else None
    source = "hub+yahoo" if mids else "hub"
    row = evaluate_macro_ticks(brent=brent, wti=wti, vix=vix, source=source)
    return _apply_and_maybe_alert(row)


def _worker() -> None:
    log_engine(
        f"GeopoliticalMonitor: armed (brent<{BRENT_COOL_USD} vix<{VIX_COOL} poll={POLL_SEC:.0f}s)"
    )
    while not _stop.is_set():
        try:
            poll_once()
        except Exception as exc:
            log_engine(f"GeopoliticalMonitor: cycle error {type(exc).__name__}: {exc}")
        _stop.wait(POLL_SEC)


def start_geopolitical_monitor() -> None:
    global _started, _thread
    if _started:
        return
    _started = True
    _stop.clear()
    _thread = threading.Thread(
        target=_worker,
        name="geopolitical-monitor",
        daemon=True,
    )
    _thread.start()


def stop_geopolitical_monitor() -> None:
    global _started, _thread
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=1.0)
    _thread = None
    _started = False
