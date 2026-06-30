"""WebSocket /ws/state — streams live agent state snapshots to cockpit clients."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.agent_state import get_agent_state, subscribe_state
from system.engine_log import log_engine

router = APIRouter()


class _StateStreamHub:
    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsub: Any = None
        self._pending: deque[dict[str, Any]] = deque(maxlen=32)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._unsub is None:
            self._unsub = subscribe_state(self._on_state_threadsafe)
        self._flush_pending()

    def _flush_pending(self) -> None:
        if self._loop is None or not self._pending:
            return
        pending = list(self._pending)
        self._pending.clear()

        def _enqueue() -> None:
            for payload in pending:
                self._deliver(payload)

        self._loop.call_soon_threadsafe(_enqueue)

    def _deliver(self, payload: dict[str, Any]) -> None:
        for queue in list(self._queues.values()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    def _on_state_threadsafe(self, payload: dict[str, Any]) -> None:
        if self._loop is None:
            self._pending.append(payload)
            return
        self._loop.call_soon_threadsafe(lambda: self._deliver(payload))

    def register(self, ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues[ws] = queue

    def unregister(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)


hub = _StateStreamHub()


def get_ws_subscriber_count() -> int:
    """Number of currently connected /ws/state WebSocket clients."""
    return len(hub._queues)


@router.websocket("/ws/state")
async def websocket_agent_state(ws: WebSocket) -> None:
    await ws.accept()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
    hub.register(ws, outbound)
    await outbound.put(get_agent_state())

    async def _reader() -> None:
        while True:
            await ws.receive_text()

    async def _writer() -> None:
        while True:
            payload = await outbound.get()
            try:
                await ws.send_json(payload)
            except Exception as exc:
                log_engine(f"/ws/state send failed: {type(exc).__name__}: {exc}")

    try:
        await asyncio.gather(_reader(), _writer())
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log_engine(f"/ws/state session error: {type(exc).__name__}: {exc}")
    finally:
        hub.unregister(ws)
