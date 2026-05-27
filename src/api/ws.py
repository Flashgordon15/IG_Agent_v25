"""
WebSocket /ws — pushes spec tick JSON on every published tick.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.snapshot_store import get_tick, subscribe

router = APIRouter()


class _WsHub:
    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsub = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._unsub is None:
            self._unsub = subscribe(self._on_tick_threadsafe)

    def _on_tick_threadsafe(self, tick: dict[str, Any]) -> None:
        if self._loop is None:
            return

        def _enqueue() -> None:
            for queue in list(self._queues.values()):
                try:
                    queue.put_nowait(tick)
                except asyncio.QueueFull:
                    pass

        self._loop.call_soon_threadsafe(_enqueue)

    def register(self, ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues[ws] = queue

    def unregister(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)

hub = _WsHub()


@router.websocket("/ws")
async def websocket_ticks(ws: WebSocket) -> None:
    await ws.accept()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
    hub.register(ws, outbound)
    await outbound.put(get_tick())

    async def _reader() -> None:
        while True:
            await ws.receive_text()

    async def _writer() -> None:
        while True:
            tick = await outbound.get()
            await ws.send_json(tick)

    try:
        await asyncio.gather(_reader(), _writer())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.unregister(ws)

