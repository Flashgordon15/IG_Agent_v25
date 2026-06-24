"""
V4 Micro-Reactor — unified async event-driven ingress + execution guard.

Single non-blocking reactor replaces competing telemetry / execution threads:
  1. Memory bound — (131072, 8) float32 matrix fully resident before network I/O
  2. Ingestion slice — throttled Yahoo + hub publish → alpha ring
  3. Execution guard — immutable recover-on-tick bridge (thread_b_alive locked true)

Trading loops consume prebaked memory via the data-isolation gasket with no
direct network requests when the gasket is active.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote as url_quote

import numpy as np
import requests

from data.ohlc_yahoo_seeder import default_spread_for_yahoo_symbol
from feeder.yahoo_quote_poller import (
    YahooQuoteSample,
    yahoo_quote_from_mid,
    yahoo_symbol_for_epic,
)
from intelligence.matrix_prebaker import MATRIX_COLS, TOTAL_CELLS
from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.ipc.ring_buffer import SOURCE_YAHOO, get_alpha_ring_buffer
from system.market_data_hub import NIGHT_MATRIX_EPICS, QuoteSnapshot, get_market_data_hub
from system.paths import data_dir, project_root

from intelligence.telemetry_circuit_breaker import (
    is_offline_replicator_mode,
    last_vector_for_epic,
    matrix_writes_frozen,
    record_feed_failure,
    record_successful_tick,
)

REACTOR_THREAD_NAME = "ig-v4-micro-reactor"
MATRIX_SHAPE = (TOTAL_CELLS, MATRIX_COLS)
_DEBOUNCE_SEC = 0.05
_DEFAULT_POLL_HZ = 1.0
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_USER_AGENT = "IG-Agent-Apex/30.0"
_FETCH_TIMEOUT_SEC = 0.75

_AUDIT_PATHS = (
    project_root() / "src" / "data" / "logs" / "self_healing_audit.log",
    data_dir() / "logs" / "self_healing_audit.log",
)
_audit_lock = threading.Lock()

_reactor_ref: V4MicroReactor | None = None
_reactor_lock = threading.Lock()
_gasket_active = False
_execution_bridge_locked = False


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_reactor_audit(record: dict[str, Any]) -> None:
    payload = dict(record)
    payload.setdefault("ts", _utc_iso())
    payload.setdefault("component", "v4_micro_reactor")
    line = json.dumps(payload, separators=(",", ":"), default=str)
    for path in _AUDIT_PATHS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _audit_lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            log_guarded_exception("v4_micro_reactor_audit", exc)


def _gasket_config(config: Any | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from system.config_loader import ConfigLoader

            config = ConfigLoader().load()
        except Exception:
            return {"enabled": True, "poll_hz_per_epic": _DEFAULT_POLL_HZ}
    block = config.get("data_isolation_gasket", {}) if hasattr(config, "get") else {}
    if not isinstance(block, dict):
        block = {}
    enabled = block.get("enabled")
    if enabled is None:
        pv2 = config.get("platform_v2", {}) if hasattr(config, "get") else {}
        enabled = bool(pv2.get("enabled", False)) if isinstance(pv2, dict) else False
    return {
        "enabled": bool(enabled),
        "poll_hz_per_epic": float(block.get("poll_hz_per_epic", _DEFAULT_POLL_HZ)),
    }


def is_telemetry_gasket_active() -> bool:
    return bool(_gasket_active)


def is_v4_execution_bridge_alive() -> bool:
    """Immutable execution bridge — locked true once reactor memory-bound."""
    return bool(_execution_bridge_locked)


def gasket_fetch_if_stale(
    epic: str,
    *,
    max_age: float | None = None,
) -> QuoteSnapshot | None:
    """Serve hub / ring-buffer quotes without IG REST when gasket is active."""
    if not is_telemetry_gasket_active():
        return None
    epic_key = str(epic or "").strip()
    hub = get_market_data_hub()
    snap = hub.get_snapshot(epic_key)
    if snap and snap.bid > 0 and snap.offer > 0:
        if max_age is None or snap.age_seconds() <= float(max_age):
            return snap
    try:
        ring = get_alpha_ring_buffer()
        row = ring.read_quote_for_epic(epic_key)
        if row is None:
            frozen = last_vector_for_epic(epic_key)
            if frozen is not None:
                return QuoteSnapshot(
                    epic=epic_key,
                    bid=frozen.bid,
                    offer=frozen.offer,
                    updated_at=frozen.frozen_at,
                    source="telemetry_gasket_replay",
                )
            return snap
        bid, offer, _seq = row
        now = time.time()
        return QuoteSnapshot(
            epic=epic_key,
            bid=bid,
            offer=offer,
            updated_at=now,
            source="telemetry_gasket_ring",
        )
    except Exception as exc:
        log_guarded_exception("gasket_fetch_if_stale", exc, epic=epic_key)
        return snap


def _fetch_yahoo_sample(epic: str) -> tuple[YahooQuoteSample | None, int | None, str]:
    symbol = yahoo_symbol_for_epic(epic)
    if not symbol:
        return None, None, "no_yahoo_symbol"
    url = _YAHOO_CHART.format(symbol=url_quote(symbol, safe=""))
    try:
        response = requests.get(
            url,
            timeout=_FETCH_TIMEOUT_SEC,
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code == 429:
            return None, 429, "http_429"
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return None, int(status) if status else None, f"http_error:{type(exc).__name__}"
    except Exception as exc:
        return None, None, f"fetch_error:{type(exc).__name__}"

    result = payload.get("chart", {}).get("result")
    if not result or not isinstance(result[0], dict):
        return None, None, "empty_chart"
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        try:
            closes = result[0]["indicators"]["quote"][0].get("close") or []
            valid = [float(c) for c in closes if c is not None]
            if valid:
                price = valid[-1]
        except (KeyError, IndexError, TypeError, ValueError):
            price = None
    try:
        mid = float(price)
    except (TypeError, ValueError):
        return None, None, "invalid_price"
    if mid <= 0:
        return None, None, "non_positive_price"
    return yahoo_quote_from_mid(epic, mid, symbol), None, "ok"


_STRESS_DURATION_SEC = 300.0
_STRESS_HZ = 50.0
_STRESS_INTERVAL_SEC = 1.0 / _STRESS_HZ
_STRESS_GOLD_EPIC = "CS.D.CFPGOLD.CFP.IP"
_STRESS_GOLD_LO = 4120.0
_STRESS_GOLD_HI = 4140.0

_stress_arm_timer: threading.Timer | None = None
_stress_until_mono: float = 0.0
_stress_thread: threading.Thread | None = None
_stress_lock = threading.Lock()
_stress_stats = {"ticks": 0, "matrix_writes": 0, "started_at": None}


def ui_stress_test_active() -> bool:
    return time.monotonic() < float(_stress_until_mono or 0.0)


def ui_stress_test_status() -> dict[str, Any]:
    return {
        "active": ui_stress_test_active(),
        "hz": _STRESS_HZ,
        "epic": _STRESS_GOLD_EPIC,
        "range": [_STRESS_GOLD_LO, _STRESS_GOLD_HI],
        "until_mono": _stress_until_mono,
        "stats": dict(_stress_stats),
    }


def _set_stress_fulfillment_flag(active: bool) -> None:
    try:
        from system.unified_fulfillment_cache import patch_fulfillment_stress_flag

        patch_fulfillment_stress_flag(
            active=active,
            hz=_STRESS_HZ,
            epic=_STRESS_GOLD_EPIC,
        )
    except Exception as exc:
        log_guarded_exception("ui_stress_fulfillment_flag", exc)


def maybe_arm_ui_stress_render_from_env(*, delay_sec: float = 8.0) -> None:
    """Schedule UI stress burst when ``IG_UI_STRESS_RENDER=1`` (idempotent)."""
    global _stress_arm_timer
    if os.environ.get("IG_UI_STRESS_RENDER", "").strip() != "1":
        return
    if ui_stress_test_active():
        return
    with _stress_lock:
        if _stress_arm_timer is not None and _stress_arm_timer.is_alive():
            return
        _stress_arm_timer = threading.Timer(delay_sec, execute_ui_stress_test_render)
        _stress_arm_timer.daemon = True
        _stress_arm_timer.start()


def execute_ui_stress_test_render() -> dict[str, Any]:
    """
    Temporary UI stress run — 50Hz Gold oscillation (4120–4140) for 5 minutes.

    Bypasses the 1Hz per-epic poll throttle and writes directly into the
    (131072, 8) float32 matrix via the alpha ring buffer.
    """
    global _stress_until_mono, _stress_thread, _stress_stats

    with _stress_lock:
        _stress_until_mono = time.monotonic() + _STRESS_DURATION_SEC
        _stress_stats = {
            "ticks": 0,
            "matrix_writes": 0,
            "started_at": _utc_iso(),
        }
        if _stress_thread is None or not _stress_thread.is_alive():
            _stress_thread = threading.Thread(
                target=_ui_stress_render_loop,
                name="ig-ui-stress-render",
                daemon=True,
            )
            _stress_thread.start()
    _set_stress_fulfillment_flag(True)
    log_engine(
        f"V4MicroReactor: UI stress render armed "
        f"{_STRESS_GOLD_EPIC} {_STRESS_GOLD_LO}-{_STRESS_GOLD_HI} @ {_STRESS_HZ}Hz "
        f"duration={_STRESS_DURATION_SEC}s"
    )
    return ui_stress_test_status()


def _ui_stress_render_loop() -> None:
    global _stress_stats
    reactor = get_v2_telemetry_daemon()
    if reactor is None:
        try:
            reactor = start_v2_telemetry_daemon()
        except Exception as exc:
            _append_reactor_audit(
                {"event": "ui_stress_no_reactor", "message": str(exc)}
            )
            return
    if reactor is None:
        return
    if not reactor._ensure_memory_bound():
        return

    t0 = time.monotonic()
    phase = 0.0
    while time.monotonic() < _stress_until_mono and not reactor._stop.is_set():
        span = _STRESS_GOLD_HI - _STRESS_GOLD_LO
        mid = _STRESS_GOLD_LO + (span * 0.5) + (span * 0.5) * np.sin(phase)
        spread = max(0.05, mid * 0.00008)
        sample = YahooQuoteSample(
            epic=_STRESS_GOLD_EPIC,
            symbol=yahoo_symbol_for_epic(_STRESS_GOLD_EPIC) or "GC=F",
            mid=float(mid),
            bid=float(mid - spread / 2.0),
            offer=float(mid + spread / 2.0),
            source="ui_stress_render",
        )
        try:
            reactor._publish_tick(sample)
            _stress_wave_matrix(reactor, _STRESS_GOLD_EPIC, float(mid), float(spread))
            with _stress_lock:
                _stress_stats["ticks"] = int(_stress_stats.get("ticks", 0)) + 1
                _stress_stats["matrix_writes"] = int(
                    _stress_stats.get("matrix_writes", 0)
                ) + 1
        except Exception as exc:
            _append_reactor_audit(
                {
                    "event": "ui_stress_tick_recover",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        phase += 0.35
        time.sleep(_STRESS_INTERVAL_SEC)

    _set_stress_fulfillment_flag(False)
    _append_reactor_audit(
        {
            "event": "ui_stress_complete",
            "duration_sec": round(time.monotonic() - t0, 2),
            "stats": dict(_stress_stats),
        }
    )


def _stress_wave_matrix(
    reactor: V4MicroReactor,
    epic: str,
    mid: float,
    spread: float,
) -> None:
    """Sweep Gold epic matrix rows with oscillating float32 anchors."""
    from intelligence.matrix_prebaker import (
        CELLS_PER_EPIC,
        COL_ATR_ANCHOR,
        COL_RSI_ANCHOR,
        COL_SAMPLES,
        epic_slot,
    )

    try:
        ring = get_alpha_ring_buffer()
        mat = ring.matrix_view()
        slot = epic_slot(epic)
        if slot is None:
            return
        base = int(slot) * int(CELLS_PER_EPIC)
        end = min(base + int(CELLS_PER_EPIC), mat.shape[0])
        for i in range(base, end, max(1, int(CELLS_PER_EPIC // 32))):
            row = mat[i]
            row[COL_ATR_ANCHOR] = np.float32(spread + (i % 7) * 0.002)
            row[COL_RSI_ANCHOR] = np.float32(50.0 + ((mid + i) % 20))
            row[COL_SAMPLES] = np.float32(max(1.0, float(row[COL_SAMPLES]) + 0.01))
        reactor._matrix = mat
    except Exception as exc:
        log_guarded_exception("ui_stress_matrix_wave", exc)


class V4MicroReactor:
    """
    Unified non-blocking micro-reactor — ingestion + execution guard in one loop.

    Memory is fully bound before any network socket opens. Each tick slice is
    wrapped in recover-on-next-tick error handling with self_healing_audit.log.
    """

    def __init__(
        self,
        *,
        epics: tuple[str, ...] | list[str] | None = None,
        poll_hz_per_epic: float = _DEFAULT_POLL_HZ,
    ) -> None:
        self._epics = tuple(epics or NIGHT_MATRIX_EPICS)
        self._interval = 1.0 / max(0.1, float(poll_hz_per_epic))
        self._last_poll: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._network_armed = False
        self._matrix: np.ndarray | None = None
        self._last_quote_seq: dict[str, int] = {}
        self._stats = {
            "polls": 0,
            "published": 0,
            "errors": 0,
            "replays": 0,
            "execution_slices": 0,
            "recoveries": 0,
        }

    @property
    def running(self) -> bool:
        return self._running

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._reactor_loop,
            name=REACTOR_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()
        log_engine(
            f"V4MicroReactor: armed epics={len(self._epics)} "
            f"poll_hz={1.0 / self._interval:.2f} matrix_shape={MATRIX_SHAPE}"
        )

    def stop(self) -> None:
        global _execution_bridge_locked
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        _execution_bridge_locked = False

    def tick_once(self) -> None:
        """Synchronous reactor pass — boot priming before background loop."""
        if not self._ensure_memory_bound():
            return
        self._prime_replay_vectors()
        now = time.monotonic()
        for epic in self._epics:
            self._maybe_poll_epic(epic, now=now)
        self._execution_guard_slice()

    def _ensure_memory_bound(self) -> bool:
        """Force (131072, 8) float32 resident in RAM before network I/O."""
        global _execution_bridge_locked
        if self._matrix is not None:
            return True

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                ring = get_alpha_ring_buffer()
                mat = ring.matrix_view()
                if mat.shape != MATRIX_SHAPE or mat.dtype != np.dtype(np.float32):
                    raise RuntimeError(
                        f"matrix shape/dtype mismatch: {mat.shape} {mat.dtype}"
                    )
                # Touch pages so allocation is fully resident before sockets open.
                _ = float(mat[0, 0])
                _ = float(mat[-1, -1])
                self._matrix = mat
                _execution_bridge_locked = True
                try:
                    if int(ring.telemetry().get("compile_generation") or 0) <= 0:
                        ring.write_matrix_generation(
                            mat.copy(),
                            vector_density=1,
                            cfg=None,
                        )
                except Exception as exc:
                    _append_reactor_audit(
                        {
                            "event": "compile_generation_bootstrap",
                            "message": str(exc),
                        }
                    )
                log_engine(
                    f"V4MicroReactor: memory bound "
                    f"cells={MATRIX_SHAPE[0]} cols={MATRIX_SHAPE[1]} "
                    f"bytes={mat.nbytes}"
                )
                return True
            except Exception as exc:
                _append_reactor_audit(
                    {
                        "event": "memory_bound_debounce",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
                if self._stop.wait(_DEBOUNCE_SEC):
                    break
        return self._matrix is not None

    def _arm_network_ingress(self) -> None:
        """Open feed/network paths only after memory is bound."""
        if self._network_armed:
            return
        self._network_armed = True
        try:
            from system.feeds.multi_feed_hub import start_racing_multi_feed_hub

            start_racing_multi_feed_hub()
            log_engine("V4MicroReactor: multi-feed hub armed post memory-bound")
        except Exception as exc:
            _append_reactor_audit(
                {
                    "event": "network_ingress_recover",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )

    def _prime_replay_vectors(self) -> None:
        """Seed hub + ring before live fetch so velocity watchdog sees ingress."""
        for epic in self._epics:
            try:
                self._replay_epic(epic)
            except Exception as exc:
                _append_reactor_audit(
                    {"event": "prime_replay", "epic": epic, "message": str(exc)}
                )

    def _reactor_loop(self) -> None:
        global _execution_bridge_locked
        if not self._ensure_memory_bound():
            _append_reactor_audit(
                {"event": "memory_bound_failed", "message": "reactor withheld"}
            )
            self._running = False
            return

        _execution_bridge_locked = True
        self._prime_replay_vectors()
        self._arm_network_ingress()

        while not self._stop.is_set():
            try:
                self._ingestion_slice()
                self._execution_guard_slice()
            except Exception as exc:
                self._stats["recoveries"] += 1
                _append_reactor_audit(
                    {
                        "event": "reactor_slice_recover",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
            if self._stop.wait(_DEBOUNCE_SEC):
                break

        _execution_bridge_locked = False
        self._running = False

    def _ingestion_slice(self) -> None:
        now = time.monotonic()
        for epic in self._epics:
            self._maybe_poll_epic(epic, now=now)

    def _maybe_poll_epic(self, epic: str, *, now: float) -> None:
        if ui_stress_test_active() and epic == _STRESS_GOLD_EPIC:
            return
        last = self._last_poll.get(epic, 0.0)
        if now - last < self._interval:
            return
        self._last_poll[epic] = now
        self._poll_epic(epic)

    def execute_ui_stress_test_render(self) -> dict[str, Any]:
        """Instance hook — delegates to module-level stress runner."""
        return execute_ui_stress_test_render()

    def _poll_epic(self, epic: str) -> None:
        self._stats["polls"] += 1
        if is_offline_replicator_mode():
            self._replay_epic(epic)
            return

        sample, http_status, reason = _fetch_yahoo_sample(epic)
        if sample is None:
            self._stats["errors"] += 1
            record_feed_failure(epic=epic, reason=reason, http_status=http_status)
            self._replay_epic(epic)
            return

        self._publish_tick(sample)
        record_successful_tick(
            epic=sample.epic,
            bid=sample.bid,
            offer=sample.offer,
            mid=sample.mid,
            spread=sample.offer - sample.bid,
        )

    def _replay_epic(self, epic: str) -> None:
        vec = last_vector_for_epic(epic)
        if vec is None:
            symbol = yahoo_symbol_for_epic(epic) or ""
            spread = default_spread_for_yahoo_symbol(symbol) if symbol else 0.0001
            try:
                from system.config_loader import get_config

                cfg = get_config()
                inst = (cfg.instruments or {}).get(epic, {}) if cfg else {}
                mid = float(inst.get("default_mid") or inst.get("mid") or 0.0)
            except Exception:
                mid = 0.0
            if mid <= 0:
                return
            sample = YahooQuoteSample(
                epic=epic,
                symbol=symbol,
                mid=mid,
                bid=mid - spread / 2.0,
                offer=mid + spread / 2.0,
                source="telemetry_gasket_seed",
            )
        else:
            self._stats["replays"] += 1
            sample = YahooQuoteSample(
                epic=vec.epic,
                symbol=yahoo_symbol_for_epic(vec.epic) or "",
                mid=vec.mid,
                bid=vec.bid,
                offer=vec.offer,
                source="telemetry_gasket_replay",
            )
        self._publish_tick(sample)

    def _publish_tick(self, sample: YahooQuoteSample) -> None:
        spread = float(sample.offer - sample.bid)
        hub = get_market_data_hub()
        hub.publish(
            sample.epic,
            sample.bid,
            sample.offer,
            source="telemetry_gasket",
        )
        try:
            ring = get_alpha_ring_buffer()
            ring.write_quote_race_win(
                sample.epic,
                bid=sample.bid,
                offer=sample.offer,
                mid=sample.mid,
                source_id=SOURCE_YAHOO,
            )
            if not matrix_writes_frozen():
                self._write_tick_to_matrix(ring, sample.epic, spread)
                ring.write_recency_calibration(
                    rsi_bias=0.0,
                    atr_bias=spread,
                    mom_bias=0.0,
                    recency_weight=1.0,
                )
        except Exception as exc:
            _append_reactor_audit(
                {
                    "event": "matrix_write_recover",
                    "epic": sample.epic,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        self._stats["published"] += 1

    @staticmethod
    def _write_tick_to_matrix(ring: Any, epic: str, spread: float) -> None:
        from intelligence.matrix_prebaker import CELLS_PER_EPIC, COL_ATR_ANCHOR, epic_slot

        slot = epic_slot(epic)
        if slot is None:
            return
        idx = int(slot) * int(CELLS_PER_EPIC)
        mat = ring.matrix_view()
        if idx < 0 or idx >= mat.shape[0]:
            return
        row = mat[idx]
        row[COL_ATR_ANCHOR] = np.float32(spread)

    def _execution_guard_slice(self) -> None:
        """
        Immutable non-blocking execution bridge — recover on next tick slice.

        Keeps thread_b_alive locked via is_v4_execution_bridge_alive() even when
        broker latency spikes or packets drop.
        """
        global _execution_bridge_locked
        _execution_bridge_locked = True
        self._stats["execution_slices"] += 1

        try:
            from datetime import datetime, timezone

            from data.models import Quote
            from system.unified_engine import get_boot_context

            ring = get_alpha_ring_buffer()
            orchestrator = getattr(get_boot_context(), "orchestrator", None)
            if orchestrator is None:
                return

            loops = list(getattr(orchestrator, "loops", []) or [])
            for loop in loops:
                epic = str(getattr(loop, "_epic", "") or "")
                if not epic:
                    continue
                sampled = ring.read_quote_for_epic(epic)
                if sampled is None:
                    continue
                bid, offer, seq = sampled
                prev = self._last_quote_seq.get(epic)
                if prev is not None and prev == seq:
                    continue
                self._last_quote_seq[epic] = seq
                quote = Quote(datetime.now(timezone.utc), bid, offer)
                run_bare = getattr(loop, "run_bare_metal_unified_tick", None)
                if callable(run_bare):
                    try:
                        run_bare(quote)
                    except Exception as exc:
                        _append_reactor_audit(
                            {
                                "event": "execution_guard_recover",
                                "epic": epic,
                                "message": f"{type(exc).__name__}: {exc}",
                            }
                        )
        except Exception as exc:
            _append_reactor_audit(
                {
                    "event": "execution_guard_recover",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )


# Backward-compatible alias for legacy imports / logs
V2TelemetryDaemon = V4MicroReactor
DAEMON_THREAD_NAME = REACTOR_THREAD_NAME


def start_v2_telemetry_daemon(config: Any | None = None) -> V4MicroReactor | None:
    global _reactor_ref, _gasket_active
    cfg = _gasket_config(config)
    if not cfg["enabled"]:
        return None
    with _reactor_lock:
        if _reactor_ref is not None and _reactor_ref.running:
            maybe_arm_ui_stress_render_from_env(delay_sec=8.0)
            return _reactor_ref
        try:
            from feeder.yahoo_quote_poller import stop_yahoo_quote_poller

            stop_yahoo_quote_poller()
        except Exception as exc:
            log_guarded_exception("telemetry_daemon_stop_yahoo", exc)
        _reactor_ref = V4MicroReactor(poll_hz_per_epic=cfg["poll_hz_per_epic"])
        _gasket_active = True
        _reactor_ref.start()
        try:
            _reactor_ref.tick_once()
        except Exception as exc:
            _append_reactor_audit(
                {"event": "boot_tick_recover", "message": f"{type(exc).__name__}: {exc}"}
            )
        if os.environ.get("IG_UI_STRESS_RENDER", "").strip() == "1":
            maybe_arm_ui_stress_render_from_env(delay_sec=8.0)
        return _reactor_ref


def stop_v2_telemetry_daemon() -> None:
    global _reactor_ref, _gasket_active, _execution_bridge_locked
    with _reactor_lock:
        if _reactor_ref is not None:
            _reactor_ref.stop()
        _reactor_ref = None
        _gasket_active = False
        _execution_bridge_locked = False


def get_v2_telemetry_daemon() -> V4MicroReactor | None:
    return _reactor_ref
