"""
WebSocket /ws — pushes spec tick JSON on every published tick.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.snapshot_store import get_tick, subscribe
from system.engine_log import log_engine

router = APIRouter()


class _WsHub:
    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsub = None
        self._pending: deque[dict[str, Any]] = deque(maxlen=64)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._unsub is None:
            self._unsub = subscribe(self._on_tick_threadsafe)
        self._flush_pending()

    def _flush_pending(self) -> None:
        if self._loop is None or not self._pending:
            return

        pending = list(self._pending)
        self._pending.clear()

        def _enqueue() -> None:
            for tick in pending:
                self._deliver(tick)

        self._loop.call_soon_threadsafe(_enqueue)

    def _deliver(self, tick: dict[str, Any]) -> None:
        for queue in list(self._queues.values()):
            try:
                queue.put_nowait(tick)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(tick)
                except asyncio.QueueFull:
                    pass

    def _on_tick_threadsafe(self, tick: dict[str, Any]) -> None:
        if self._loop is None:
            self._pending.append(tick)
            return

        self._loop.call_soon_threadsafe(lambda: self._deliver(tick))

    def register(self, ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues[ws] = queue

    def unregister(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)


hub = _WsHub()


@router.websocket("/ws")
async def websocket_ticks(ws: WebSocket) -> None:
    await ws.accept()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    hub.register(ws, outbound)
    await outbound.put(get_tick())

    async def _reader() -> None:
        while True:
            await ws.receive_text()

    async def _writer() -> None:
        while True:
            tick = await outbound.get()
            try:
                await ws.send_json(tick)
            except Exception as e:
                log_engine(f"WebSocket send_json failed: {type(e).__name__}: {e}")

    try:
        await asyncio.gather(_reader(), _writer())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log_engine(f"WebSocket session error: {type(e).__name__}: {e}")
    finally:
        hub.unregister(ws)
