"""Gate 3 — streaming connection without resetting hub quote state."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from system.boot.context import BootContext
from system.engine_log import log_engine
from system.system_state import BootPhase, GateStatus, SystemState, get_system_state

_G3_TIMEOUT_SEC = 120.0
_G3_POLL_SEC = 0.5
_QUOTE_MAX_AGE_SEC = 45.0


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
    except Exception:
        pass
    return bool(getattr(client, "_running", False))


def _first_live_tick_epic(epics: list[str]) -> str | None:
    from system.market_data_hub import get_market_data_hub

    hub = get_market_data_hub()
    for epic in epics:
        snap = hub.get_snapshot(epic)
        if snap is None or snap.bid <= 0 or snap.offer <= 0:
            continue
        if snap.age_seconds() <= _QUOTE_MAX_AGE_SEC:
            return epic
    return None


def _open_market_epics(epics: list[str]) -> list[str]:
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

        from api.snapshot_store import wire_hub_quotes_to_dashboard
        from execution.position_protect_hub import wire_hub_quotes_to_position_protect
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

        transport_mode, _transport_reason = resolve_streaming_transport(
            cfg.streaming_transport
        )

        wire_hub_quotes_to_dashboard(min_interval=0.25)
        wire_hub_quotes_to_position_protect(min_interval=0.05)

        if cfg.get("intelligence_layer", {}).get("enabled"):
            try:
                from intelligence.intelligence_worker import wire_intelligence_to_hub

                wire_intelligence_to_hub()
                log_engine("Gate3: intelligence hub subscriber wired")
            except Exception as exc:
                log_engine(
                    f"Gate3: intelligence hub wire skipped: {type(exc).__name__}: {exc}"
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

        while time.monotonic() < deadline:
            heartbeat_ok = _stream_heartbeat_ok(client)
            tick_epic = _first_live_tick_epic(open_epics if open_epics else epics)

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
