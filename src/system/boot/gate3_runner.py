"""Gate 3 — streaming connection without resetting hub quote state."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from system.boot.context import BootContext
from system.engine_log import log_engine
from system.system_state import BootPhase, GateStatus, SystemState, get_system_state

_G3_TIMEOUT_SEC = 120.0
_G3_POLL_SEC = 0.5
_QUOTE_MAX_AGE_SEC = 45.0
_G3_HEAL_MIN_ELAPSED_SEC = 8.0
_G3_HEAL_FORCE_SEC = 30.0
_G3_HEAL_LOCK_TIMEOUT_SEC = 2.0
_gate3_started_mono: float | None = None
_g3_cancel_event = threading.Event()
_g3_heal_sidechannel: dict[str, Any] | None = None
_g3_heal_sc_lock = threading.Lock()


def cancel_g3_runner() -> None:
    """Cooperative cancel — hydration worker timeout stops the Yahoo wait loop."""
    _g3_cancel_event.set()


def note_g3_started() -> None:
    global _gate3_started_mono
    _g3_cancel_event.clear()
    _gate3_started_mono = time.monotonic()


def _hub_fresh_count(*, relaxed: bool = False) -> int:
    from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

    hub = get_market_data_hub()
    checker = _fresh_hub_epic_relaxed if relaxed else _fresh_hub_epic
    return sum(1 for epic in NIGHT_MATRIX_EPICS if checker(epic, hub=hub))


def _schedule_async_g3_boot_heal(*, epic: str | None, detail: str) -> None:
    """Complete G3 on a helper thread when the hydration worker cannot take the state lock."""

    def _worker() -> None:
        state = get_system_state()
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if state.gate_complete("G3"):
                return
            if state._lock.acquire(timeout=2.0):
                try:
                    if not state.gate_complete("G3"):
                        _apply_g3_boot_heal(state, epic=epic, detail=detail)
                    return
                finally:
                    state._lock.release()
            time.sleep(0.25)

    threading.Thread(
        target=_worker,
        name="g3-async-heal",
        daemon=True,
    ).start()


def _schedule_g3_heal_sidechannel(*, epic: str | None, detail: str) -> None:
    global _g3_heal_sidechannel
    with _g3_heal_sc_lock:
        _g3_heal_sidechannel = {"epic": epic, "detail": detail}


def _consume_g3_heal_sidechannel() -> dict[str, Any] | None:
    global _g3_heal_sidechannel
    with _g3_heal_sc_lock:
        payload = _g3_heal_sidechannel
        _g3_heal_sidechannel = None
        return payload


def _first_live_tick_epic_relaxed(epics: list[str]) -> str | None:
    from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

    hub = get_market_data_hub()
    seen: set[str] = set()
    for epic in list(epics) + list(NIGHT_MATRIX_EPICS):
        key = str(epic or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if _fresh_hub_epic_relaxed(key, hub=hub):
            return key
    return None


def _feed_ready_probe(*, relaxed: bool = False) -> tuple[bool, str | None]:
    """Feed readiness without self-HTTP or orchestrator locks (avoids deadlock)."""
    epic_fn = _first_live_tick_epic_relaxed if relaxed else _first_live_tick_epic
    if _hub_fresh_count(relaxed=relaxed) >= 1:
        return True, epic_fn([])
    return False, None


def _apply_g3_boot_heal(state: SystemState, *, epic: str | None, detail: str) -> None:
    from system.boot.gate_sideband import mark_gate_sideband
    from system.stream_ready import signal_stream_ready

    mark_gate_sideband("G3")
    signal_stream_ready(source=f"gate3:boot_heal:{detail}")
    if not state._lock.acquire(timeout=3.0):
        log_engine(f"Gate3: boot heal sideband set — lock busy detail={detail}")
        _schedule_async_g3_boot_heal(epic=epic, detail=detail)
        return
    try:
        streaming = state._snapshot.streaming
        hb_ok = bool(streaming.heartbeat_ok) if streaming is not None else False
        if state._snapshot.gates["G3"].status == GateStatus.COMPLETE and hb_ok:
            return
        state.update_state(
            BootPhase.G3_STREAMING,
            60,
            "Yahoo Reference Live",
            gates_dict=None,
            streaming={
                "transport": "yahoo",
                "heartbeat_ok": True,
                "first_tick_epic": epic,
                "first_tick_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        state.mark_gate_complete("G3", detail=f"boot_heal_{detail}")
    finally:
        state._lock.release()
    log_engine(f"Gate3: boot heal — hub fresh epic={epic or 'multi'} detail={detail}")

    def _wire_after_heal() -> None:
        try:
            from api.snapshot_store import wire_hub_quotes_to_dashboard
            from execution.position_protect_hub import wire_hub_quotes_to_position_protect

            wire_hub_quotes_to_dashboard(min_interval=0.25)
            wire_hub_quotes_to_position_protect(min_interval=0.05)
        except Exception:
            pass
        try:
            from system.agent_execution_mode import schedule_post_g3_execution_arming

            schedule_post_g3_execution_arming()
        except Exception:
            pass

    threading.Thread(target=_wire_after_heal, name="g3-heal-wire", daemon=True).start()


def _g3_should_exit(state: SystemState) -> bool:
    from system.boot.gate_sideband import gate_is_done

    if _g3_cancel_event.is_set():
        return True
    return gate_is_done(state, "G3")


def try_heal_stuck_g3(*, min_elapsed_sec: float = _G3_HEAL_MIN_ELAPSED_SEC) -> bool:
    """Unblock boot when G3 runner wedged but hub already has fresh quotes."""
    from system.boot.gate_sideband import gate_is_done
    from system.system_state import get_system_state

    state = get_system_state()
    if gate_is_done(state, "G3"):
        return False
    snap = state.try_snapshot(timeout=0.25)
    g3_status = ""
    boot_phase = ""
    if snap is not None:
        g3 = (snap.get("gates") or {}).get("G3") or {}
        g3_status = str(g3.get("status") or "").lower()
        boot_phase = str(snap.get("phase") or "").upper()
    started_mono = _gate3_started_mono
    boot_elapsed = (
        time.time() - float(snap.get("started_at_epoch") or 0) if snap else 0.0
    )
    if started_mono is not None:
        g3_elapsed = time.monotonic() - started_mono
    else:
        g3_elapsed = boot_elapsed
    force = g3_elapsed >= _G3_HEAL_FORCE_SEC or boot_elapsed >= 30.0
    healable = (
        g3_status == "running"
        or boot_phase == BootPhase.G3_STREAMING
        or force
    )
    if not healable:
        return False
    if g3_elapsed < float(min_elapsed_sec) and boot_elapsed < 30.0:
        feed_ready, _ = _feed_ready_probe(relaxed=True)
        if not feed_ready:
            return False
    feed_ready, epic = _feed_ready_probe(relaxed=True)
    if not feed_ready and not force:
        return False
    heal_detail = "hub_fresh" if feed_ready else "force_timeout"
    _apply_g3_boot_heal(state, epic=epic, detail=heal_detail)
    return True


def _transport_label(client: Any) -> str:
    label = getattr(client, "transport_label", "stream")
    if callable(label):
        return str(label())
    return str(label)


def _stream_heartbeat_ok(client: Any) -> bool:
    if client is None:
        return False
    if getattr(client, "_first_tick_received", False):
        return True
    try:
        from ig_api.streaming_client import ConnectionState

        state = getattr(client, "state", None)
        if state == ConnectionState.CONNECTED:
            return True
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("gate3_runner", exc)
    return bool(getattr(client, "_running", False))


def _fresh_hub_epic(epic: str, *, hub: Any | None = None) -> bool:
    from system.market_data_hub import get_market_data_hub

    hub = hub or get_market_data_hub()
    snap = hub.get_snapshot(epic)
    if snap is None or snap.bid <= 0 or snap.offer <= 0:
        return False
    return snap.age_seconds() <= _QUOTE_MAX_AGE_SEC


def _fresh_hub_epic_relaxed(epic: str, *, hub: Any | None = None) -> bool:
    """Align heal probes with health_light — bid-only, 60s max age."""
    from system.market_data_hub import get_market_data_hub

    hub = hub or get_market_data_hub()
    snap = hub.get_snapshot(epic)
    if snap is None or float(getattr(snap, "bid", 0) or 0) <= 0:
        return False
    return snap.age_seconds() <= 60.0


def _first_live_tick_epic(epics: list[str]) -> str | None:
    from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

    hub = get_market_data_hub()
    seen: set[str] = set()
    candidates: list[str] = []
    for epic in list(epics) + list(NIGHT_MATRIX_EPICS):
        key = str(epic or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(key)
    for epic in candidates:
        if _fresh_hub_epic(epic, hub=hub):
            return epic
    return None


def _hub_feed_ready() -> tuple[bool, str | None]:
    """Hub-only freshness — avoids orchestrator lock during G3 wait."""
    try:
        from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

        hub = get_market_data_hub()
        fresh = sum(1 for epic in NIGHT_MATRIX_EPICS if _fresh_hub_epic(epic, hub=hub))
        if fresh >= 1:
            return True, _first_live_tick_epic([])
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("gate3_hub_feed_ready", exc)
    return False, None


def _orchestrator_feed_ready() -> tuple[bool, str | None]:
    """Prefer hub scan; fall back to orchestrator telemetry when hub is empty."""
    ready, epic = _hub_feed_ready()
    if ready:
        return ready, epic
    try:
        from system.feeds.data_feed_orchestrator import signal_feed_health_ok

        if signal_feed_health_ok():
            return True, _first_live_tick_epic([])
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("gate3_orchestrator_feed_ready", exc)
    return False, None


def _open_market_epics(epics: list[str]) -> list[str]:
    try:
        from system.agent_execution_mode import force_market_open_active

        if force_market_open_active():
            return list(epics)
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("gate3_runner", exc)
    try:
        from system.market_watch.calendar import is_market_open

        return [e for e in epics if is_market_open(e)]
    except Exception:
        return list(epics)


class Gate3Runner:
    """Initialize IG streaming and wait for heartbeat + first live tick."""

    def __init__(
        self,
        state: SystemState | None = None,
        context: BootContext | None = None,
        *,
        timeout_sec: float = _G3_TIMEOUT_SEC,
    ) -> None:
        self._state = state or get_system_state()
        self._context = context or BootContext()
        self._timeout_sec = float(timeout_sec)

    def run(self) -> None:
        if _g3_should_exit(self._state):
            return
        note_g3_started()
        self._state.update_state(
            BootPhase.G3_STREAMING,
            40,
            "Streaming Initialization",
            gates_dict=None,
            streaming={"transport": "pending", "heartbeat_ok": False},
        )

        try:
            self._execute()
        except Exception as exc:
            message = f"Streaming initialization failed: {type(exc).__name__}: {exc}"
            log_engine(f"Gate3 FATAL: {message}")
            self._state.mark_gate_failed(
                "G3",
                error=message,
                detail="Stream connection did not stabilize",
            )

    def _execute(self) -> None:
        cfg = self._context.config
        rest = self._context.rest_client
        if cfg is None or rest is None:
            raise RuntimeError(
                "Gate 3 requires config and authenticated rest_client from Gate 2"
            )

        log_engine(
            f"Gate3: using BootContext rest_client={type(rest).__name__} "
            f"config={'ok' if cfg is not None else 'missing'}"
        )

        from feeder.pricing_transport import reference_transport_is_yahoo
        from ig_api.streaming_factory import resolve_streaming_transport
        from runtime.agent_bootstrap import start_market_stream
        from trading.instrument_registry import InstrumentRegistry

        reg = InstrumentRegistry(cfg.as_dict())
        epics = [
            str(inst.get("epic") or "").strip()
            for _iid, inst in reg.get_enabled_with_ids()
            if str(inst.get("epic") or "").strip()
        ]
        if not epics:
            epics = [str(cfg.epic)]
        self._context.epics = epics

        if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
            from system.stream_ready import signal_stream_ready

            signal_stream_ready(source="test_harness")
            self._state.update_state(
                BootPhase.G3_STREAMING,
                60,
                "Harness Replay (no live socket)",
                gates_dict=None,
                streaming={
                    "transport": "harness_replay",
                    "heartbeat_ok": True,
                    "first_tick_epic": epics[0] if epics else None,
                    "first_tick_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "market_closed_exempt": False,
                },
            )
            log_engine(
                f"Gate3: test harness — live websocket bypassed epics={len(epics)}"
            )
            return

        use_yahoo = reference_transport_is_yahoo(cfg)
        try:
            from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

            if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
                use_yahoo = False
        except Exception as exc:
            from system.guard.runtime_guard import log_guarded_exception

            log_guarded_exception("gate3_runner", exc)

        if use_yahoo:
            shadow_timeout = 45.0 if self._context.config else self._timeout_sec
            try:
                from system.node_profile import is_shadow_node

                if is_shadow_node():
                    shadow_timeout = min(self._timeout_sec, 45.0)
            except Exception as exc:
                from system.guard.runtime_guard import log_guarded_exception

                log_guarded_exception("gate3_runner", exc)
            prev = self._timeout_sec
            self._timeout_sec = shadow_timeout
            try:
                self._execute_yahoo_reference(epics)
            finally:
                self._timeout_sec = prev
            return

        transport_mode, _transport_reason = resolve_streaming_transport(
            cfg.streaming_transport
        )

        client = start_market_stream(
            cfg,
            rest_client=rest,
            clear_stream_ready=False,
        )
        if client is None:
            raise RuntimeError("Stream client could not be created — invalid session or credentials")

        self._context.stream_client = client
        label = _transport_label(client)
        log_engine(f"Gate3: stream client started transport={label} epics={epics}")

        self._state.update_state(
            BootPhase.G3_STREAMING,
            48,
            "Streaming Initialization",
            gates_dict=None,
            streaming={"transport": label, "heartbeat_ok": False},
        )

        open_epics = _open_market_epics(epics)
        market_closed_exempt = len(open_epics) == 0
        deadline = time.monotonic() + self._timeout_sec
        first_tick_epic: str | None = None
        first_tick_at: str | None = None
        fast_hydration_deadline = time.monotonic() + 5.0
        fast_hydration_tried = False

        while time.monotonic() < deadline:
            if _g3_should_exit(self._state):
                return
            sc = _consume_g3_heal_sidechannel()
            if sc:
                first_tick_epic = sc.get("epic")
                first_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                log_engine(
                    f"Gate3: stream side-channel heal epic={first_tick_epic or 'multi'}"
                )
                break

            heartbeat_ok = _stream_heartbeat_ok(client)
            tick_epic = _first_live_tick_epic(open_epics if open_epics else epics)

            if (
                not fast_hydration_tried
                and tick_epic is None
                and not market_closed_exempt
                and time.monotonic() >= fast_hydration_deadline
            ):
                fast_hydration_tried = True
                try:
                    from system.fast_stream_hydration import fast_stream_hydration_fallback

                    hydration = fast_stream_hydration_fallback(
                        rest,
                        cfg=cfg,
                        epics=epics,
                    )
                    if hydration.get("first_tick_epic"):
                        tick_epic = str(hydration.get("first_tick_epic"))
                        log_engine(
                            f"Gate3: fast-stream hydration mode={hydration.get('mode')} "
                            f"epic={tick_epic}"
                        )
                except Exception as exc:
                    log_engine(
                        f"Gate3: fast-stream hydration skipped: "
                        f"{type(exc).__name__}: {exc}"
                    )

            if not _g3_should_exit(self._state):
                if self._state._lock.acquire(timeout=0.05):
                    try:
                        self._state.update_state(
                            BootPhase.G3_STREAMING,
                            52 if heartbeat_ok else 48,
                            "Streaming Initialization",
                            gates_dict=None,
                            streaming={
                                "transport": label,
                                "heartbeat_ok": heartbeat_ok,
                                "first_tick_epic": tick_epic,
                                "first_tick_at": first_tick_at,
                                "market_closed_exempt": market_closed_exempt,
                            },
                        )
                    finally:
                        self._state._lock.release()

            tick_ok = tick_epic is not None or market_closed_exempt
            if heartbeat_ok and tick_ok:
                first_tick_epic = tick_epic
                if tick_epic:
                    first_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                elif market_closed_exempt:
                    first_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                break

            time.sleep(_G3_POLL_SEC)
        else:
            raise TimeoutError(
                f"no stable stream within {self._timeout_sec:.0f}s "
                f"(heartbeat={_stream_heartbeat_ok(client)}, "
                f"tick={first_tick_epic is not None})"
            )

        from system.stream_ready import signal_stream_ready

        if not market_closed_exempt:
            signal_stream_ready(source=f"gate3:{label}")
        else:
            signal_stream_ready(source="gate3:market_closed_exempt")

        self._state.update_state(
            BootPhase.G3_STREAMING,
            60,
            "Streaming Live",
            gates_dict={
                gid: (
                    self._state.snapshot_model().gates[gid].to_dict()
                    if gid != "G3"
                    else {
                        "status": GateStatus.RUNNING,
                        "detail": f"Stream live ({label})",
                    }
                )
                for gid in ("G1", "G2", "G3", "G4", "G5")
            },
            streaming={
                "transport": label,
                "heartbeat_ok": True,
                "first_tick_epic": first_tick_epic,
                "first_tick_at": first_tick_at,
                "market_closed_exempt": market_closed_exempt,
            },
        )
        log_engine(
            f"Gate3: stream confirmed heartbeat_ok=True "
            f"first_tick={first_tick_epic or 'market_closed_exempt'}"
        )
        self._wire_hub_subscribers()
        try:
            from system.agent_execution_mode import schedule_post_g3_execution_arming

            schedule_post_g3_execution_arming()
        except Exception:
            pass

    def _wire_hub_subscribers(self) -> None:
        """Wire hub listeners after G3 confirms feeds — avoids publish/re-entrant deadlocks."""
        from api.snapshot_store import wire_hub_quotes_to_dashboard
        from execution.position_protect_hub import wire_hub_quotes_to_position_protect

        wire_hub_quotes_to_dashboard(min_interval=0.25)
        wire_hub_quotes_to_position_protect(min_interval=0.05)
        cfg = self._context.config
        if cfg is not None and cfg.get("intelligence_layer", {}).get("enabled"):
            try:
                from intelligence.intelligence_worker import wire_intelligence_to_hub

                wire_intelligence_to_hub()
                log_engine("Gate3: intelligence hub subscriber wired")
            except Exception as exc:
                log_engine(
                    f"Gate3: intelligence hub wire skipped: {type(exc).__name__}: {exc}"
                )

    def _execute_yahoo_reference(self, epics: list[str]) -> None:
        from feeder.yahoo_quote_poller import get_yahoo_quote_poller
        from system.feeds.data_feed_orchestrator import start_data_feed_orchestrator
        from system.stream_ready import signal_stream_ready

        cfg = self._context.config
        label = "yahoo"
        log_engine(f"Gate3: data-feed orchestrator (Yahoo primary) epics={len(epics)}")

        self._state.update_state(
            BootPhase.G3_STREAMING,
            48,
            "Yahoo Reference Pricing",
            gates_dict=None,
            streaming={"transport": label, "heartbeat_ok": False},
        )

        threading.Thread(
            target=lambda: start_data_feed_orchestrator(epics, cfg=cfg),
            name="g3-feed-arm",
            daemon=True,
        ).start()
        time.sleep(0.35)

        poller = get_yahoo_quote_poller()

        first_tick_epic: str | None = None
        first_tick_at: str | None = None

        def _finish_yahoo_gate(*, tick_epic: str | None, detail: str) -> None:
            nonlocal first_tick_epic, first_tick_at
            if _g3_should_exit(self._state):
                return
            from system.boot.gate_sideband import mark_gate_sideband

            mark_gate_sideband("G3")
            first_tick_epic = tick_epic
            first_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            signal_stream_ready(source=f"gate3:yahoo:{detail}")
            self._context.stream_client = None
            if not self._state._lock.acquire(timeout=3.0):
                log_engine(f"Gate3: finish deferred sideband detail={detail}")
                _schedule_async_g3_boot_heal(epic=tick_epic, detail=detail)
                return
            try:
                streaming = self._state._snapshot.streaming
                hb_ok = bool(streaming.heartbeat_ok) if streaming is not None else False
                if (
                    self._state._snapshot.gates["G3"].status == GateStatus.COMPLETE
                    and hb_ok
                ):
                    return
                self._state.update_state(
                    BootPhase.G3_STREAMING,
                    60,
                    "Yahoo Reference Live",
                    gates_dict=None,
                    streaming={
                        "transport": label,
                        "heartbeat_ok": True,
                        "first_tick_epic": first_tick_epic,
                        "first_tick_at": first_tick_at,
                        "market_closed_exempt": len(_open_market_epics(epics)) == 0,
                    },
                )
                self._state.mark_gate_complete("G3", detail=detail)
            finally:
                self._state._lock.release()
            log_engine(
                f"Gate3: Yahoo reference confirmed detail={detail} epic={tick_epic or 'exempt'}"
            )

            def _post_finish() -> None:
                self._wire_hub_subscribers()
                try:
                    from system.agent_execution_mode import schedule_post_g3_execution_arming

                    schedule_post_g3_execution_arming()
                except Exception:
                    pass

            threading.Thread(target=_post_finish, name="g3-finish-wire", daemon=True).start()

        # Fast-path: orchestrator sync bootstrap or racing hub already published.
        feed_ready, fast_epic = _feed_ready_probe(relaxed=True)
        if feed_ready:
            _finish_yahoo_gate(
                tick_epic=fast_epic or _first_live_tick_epic(epics),
                detail="orchestrator_fast_path",
            )
            if _g3_should_exit(self._state):
                return

        if poller is not None:
            threading.Thread(
                target=poller.poll_all,
                name="g3-yahoo-poll-once",
                daemon=True,
            ).start()

        open_epics = _open_market_epics(epics)
        market_closed_exempt = len(open_epics) == 0
        deadline = time.monotonic() + self._timeout_sec
        last_progress = 0.0
        last_streaming_sig: tuple[Any, ...] = ()

        while time.monotonic() < deadline:
            if _g3_should_exit(self._state):
                return
            g3_elapsed = (
                time.monotonic() - _gate3_started_mono
                if _gate3_started_mono is not None
                else 0.0
            )
            if g3_elapsed >= _G3_HEAL_FORCE_SEC:
                feed_ready, force_epic = _feed_ready_probe(relaxed=True)
                _finish_yahoo_gate(
                    tick_epic=force_epic or _first_live_tick_epic_relaxed(epics),
                    detail="runner_force_30s",
                )
                if _g3_should_exit(self._state):
                    return
            sc = _consume_g3_heal_sidechannel()
            if sc:
                _finish_yahoo_gate(
                    tick_epic=sc.get("epic"),
                    detail=str(sc.get("detail") or "sidechannel_heal"),
                )
                return

            feed_ready, orch_epic = _feed_ready_probe(relaxed=True)
            if feed_ready:
                _finish_yahoo_gate(
                    tick_epic=orch_epic or _first_live_tick_epic_relaxed(epics),
                    detail="hub_fresh_loop",
                )
                if _g3_should_exit(self._state):
                    return

            tick_epic = _first_live_tick_epic(open_epics if open_epics else epics)
            if tick_epic is None:
                tick_epic = _first_live_tick_epic(epics)
            if tick_epic is None:
                tick_epic = _first_live_tick_epic_relaxed(epics)
            if tick_epic is None and feed_ready:
                tick_epic = orch_epic

            if tick_epic is not None or market_closed_exempt or feed_ready:
                first_tick_epic = tick_epic
                first_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if feed_ready and first_tick_epic is None:
                    log_engine(
                        "Gate3: Yahoo reference — hub fresh feeds "
                        "(no single epic required)"
                    )
                break

            now_mono = time.monotonic()
            if now_mono - last_progress >= 2.0:
                if _g3_should_exit(self._state):
                    return
                sc = _consume_g3_heal_sidechannel()
                if sc:
                    _finish_yahoo_gate(
                        tick_epic=sc.get("epic"),
                        detail=str(sc.get("detail") or "sidechannel_heal"),
                    )
                    return

                poller_polls = 0
                if poller is not None:
                    try:
                        poller_polls = int(poller.stats().get("polls") or 0)
                    except Exception:
                        poller_polls = 0
                heartbeat_ok = feed_ready or (
                    poller is not None
                    and poller.running
                    and (tick_epic is not None or poller_polls > 0)
                )
                streaming_sig = (
                    heartbeat_ok,
                    tick_epic,
                    first_tick_at,
                    market_closed_exempt,
                )
                if (
                    not _g3_should_exit(self._state)
                    and streaming_sig != last_streaming_sig
                ):
                    last_streaming_sig = streaming_sig
                    if self._state._lock.acquire(timeout=0.05):
                        try:
                            if self._state._snapshot.gates["G3"].status == GateStatus.COMPLETE:
                                last_progress = now_mono
                                continue
                            self._state.update_state(
                                BootPhase.G3_STREAMING,
                                52 if (tick_epic or feed_ready or heartbeat_ok) else 48,
                                "Yahoo Reference Pricing",
                                gates_dict=None,
                                streaming={
                                    "transport": label,
                                    "heartbeat_ok": heartbeat_ok,
                                    "first_tick_epic": tick_epic,
                                    "first_tick_at": first_tick_at,
                                    "market_closed_exempt": market_closed_exempt,
                                },
                            )
                        finally:
                            self._state._lock.release()
                last_progress = now_mono

            time.sleep(_G3_POLL_SEC)
        else:
            tick_epic = _first_live_tick_epic(epics)
            if tick_epic:
                first_tick_epic = tick_epic
                first_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                log_engine(
                    f"Gate3: Yahoo reference timeout — accepting hub tick epic={tick_epic}"
                )
            else:
                feed_ready, tick_epic = _feed_ready_probe(relaxed=True)
                if feed_ready:
                    log_engine(
                        "Gate3: Yahoo timeout bypass — orchestrator has fresh primary feed"
                    )
                    _finish_yahoo_gate(
                        tick_epic=tick_epic or _first_live_tick_epic(epics),
                        detail="orchestrator_degraded_accept",
                    )
                    return
                if poller is not None:
                    raise TimeoutError(
                        f"no Yahoo reference tick within {self._timeout_sec:.0f}s "
                        f"(polls={poller.stats().get('polls', 0)})"
                    )
                raise TimeoutError(
                    f"no Yahoo reference tick within {self._timeout_sec:.0f}s (poller missing)"
                )

        _finish_yahoo_gate(
            tick_epic=first_tick_epic,
            detail="yahoo_reference_pricing_live",
        )
