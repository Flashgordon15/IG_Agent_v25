"""
HARDENED_TESTBED loopback transport — zero outbound IG/Yahoo network.

Reads replay ticks from ``testbed_replay.jsonl`` and exposes a local Unix socket
for instant fill registration against the current replayed mid.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ig_api.streaming_client import ConnectionState, PriceUpdate
from system.engine_log import log_engine


@dataclass
class TestbedFill:
    deal_id: str
    epic: str
    side: str
    size: float
    price: float
    ts: float


class TestbedLoopbackTransport:
    """File-feed + Unix-socket fill adapter — drop-in for IGStreamingClient in testbed."""

    transport_label = "testbed_loopback"

    def __init__(self, *, poll_interval_seconds: float = 0.05) -> None:
        self._poll_interval = max(0.01, float(poll_interval_seconds))
        self._state = ConnectionState.DISCONNECTED
        self._on_price: Callable[[PriceUpdate], None] | None = None
        self._epics: set[str] = set()
        self._thread: threading.Thread | None = None
        self._socket_thread: threading.Thread | None = None
        self._running = False
        self._stop = threading.Event()
        self._last_mid: dict[str, float] = {}
        self._feed_offset = 0
        self._lock = threading.RLock()

    @property
    def state(self) -> ConnectionState:
        return self._state

    def on_price(self, callback: Callable[[PriceUpdate], None]) -> None:
        self._on_price = callback

    def on_account(self, callback: Callable[[Any], None]) -> None:
        del callback  # not used in replay testbed

    def on_state_change(self, callback: Callable[[ConnectionState], None]) -> None:
        del callback

    def connect(self) -> None:
        if self._running:
            return
        from system.testbed_firewall import (
            testbed_ledger_path,
            testbed_replay_feed_path,
            testbed_replay_socket_path,
        )

        self._init_ledger(testbed_ledger_path())
        testbed_replay_feed_path().touch(exist_ok=True)
        testbed_replay_socket_path().parent.mkdir(parents=True, exist_ok=True)
        try:
            if testbed_replay_socket_path().exists():
                testbed_replay_socket_path().unlink()
        except OSError:
            pass

        self._running = True
        self._stop.clear()
        self._state = ConnectionState.CONNECTING
        self._thread = threading.Thread(
            target=self._replay_loop,
            name="testbed-replay-feed",
            daemon=True,
        )
        self._socket_thread = threading.Thread(
            target=self._fill_socket_loop,
            name="testbed-fill-socket",
            daemon=True,
        )
        self._thread.start()
        self._socket_thread.start()
        self._state = ConnectionState.CONNECTED
        log_engine(
            "TestbedLoopbackTransport: armed — replay feed + fill socket (no outbound network)"
        )

    def disconnect(self) -> None:
        self._stop.set()
        self._running = False
        self._state = ConnectionState.DISCONNECTED
        for t in (self._thread, self._socket_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

    def subscribe_market(self, epic: str) -> None:
        key = str(epic or "").strip()
        if key:
            self._epics.add(key)

    def subscribe_account_summary(self) -> None:
        return

    def _init_ledger(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS testbed_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id TEXT NOT NULL,
                    epic TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size REAL NOT NULL,
                    price REAL NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            conn.commit()

    def register_fill(
        self,
        *,
        epic: str,
        side: str,
        size: float,
        price: float | None = None,
        deal_id: str | None = None,
    ) -> TestbedFill:
        """Register a simulated fill at the current replay mid (instant ledger write)."""
        from system.testbed_firewall import testbed_ledger_path

        key = str(epic or "").strip()
        px = float(price if price is not None else self._last_mid.get(key, 0.0))
        if px <= 0:
            raise ValueError(f"testbed fill rejected — no replay mid for epic={key}")
        fill = TestbedFill(
            deal_id=str(deal_id or f"TB-{int(time.time() * 1000)}"),
            epic=key,
            side=str(side or "").upper(),
            size=float(size),
            price=px,
            ts=time.time(),
        )
        with sqlite3.connect(str(testbed_ledger_path())) as conn:
            conn.execute(
                """
                INSERT INTO testbed_fills (deal_id, epic, side, size, price, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (fill.deal_id, fill.epic, fill.side, fill.size, fill.price, fill.ts),
            )
            conn.commit()
        log_engine(
            f"TestbedLoopbackTransport: fill registered {fill.side} {fill.size} "
            f"{fill.epic} @ {fill.price} deal={fill.deal_id}"
        )
        return fill

    def _emit_tick(
        self,
        epic: str,
        bid: float,
        offer: float,
        *,
        quote_ts: float | None = None,
    ) -> None:
        if epic not in self._epics and self._epics:
            return
        mid = (bid + offer) * 0.5
        with self._lock:
            self._last_mid[epic] = mid
        cb = self._on_price
        if cb is None:
            return
        ts = float(quote_ts if quote_ts is not None else time.time())
        try:
            from simulation.replay_clock import set_replay_time

            set_replay_time(ts)
        except Exception:
            pass
        cb(
            PriceUpdate(
                epic=epic,
                bid=bid,
                offer=offer,
                timestamp=ts,
            )
        )
        try:
            from system.stream_ready import signal_stream_ready

            signal_stream_ready(source=f"testbed_replay:{epic}")
        except Exception:
            pass

    def _replay_loop(self) -> None:
        from system.testbed_firewall import testbed_replay_feed_path

        feed = testbed_replay_feed_path()
        while not self._stop.is_set():
            try:
                if feed.is_file():
                    with feed.open("r", encoding="utf-8") as fh:
                        fh.seek(self._feed_offset)
                        for line in fh:
                            if self._stop.is_set():
                                break
                            line = line.strip()
                            if not line:
                                continue
                            self._feed_offset = fh.tell()
                            try:
                                row = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if str(row.get("type") or "") != "tick":
                                continue
                            epic = str(row.get("epic") or "")
                            bid = float(row.get("bid") or 0)
                            offer = float(row.get("offer") or bid)
                            quote_ts = _parse_replay_timestamp(
                                row.get("timestamp", row.get("ts"))
                            )
                            if epic and bid > 0 and offer > 0:
                                self._emit_tick(
                                    epic,
                                    bid,
                                    offer,
                                    quote_ts=quote_ts,
                                )
            except OSError:
                pass
            if self._stop.wait(self._poll_interval):
                break

    def _fill_socket_loop(self) -> None:
        from system.testbed_firewall import testbed_replay_socket_path

        sock_path = testbed_replay_socket_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(sock_path))
            server.listen(4)
            server.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    conn, _addr = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with conn:
                    conn.settimeout(2.0)
                    try:
                        data = conn.recv(65536).decode("utf-8", errors="replace")
                        payload = json.loads(data.strip() or "{}")
                        if str(payload.get("op") or "") == "fill":
                            fill = self.register_fill(
                                epic=str(payload.get("epic") or ""),
                                side=str(payload.get("side") or "BUY"),
                                size=float(payload.get("size") or 0),
                                price=(
                                    float(payload["price"])
                                    if payload.get("price") is not None
                                    else None
                                ),
                                deal_id=str(payload.get("deal_id") or "") or None,
                            )
                            conn.sendall(
                                json.dumps({"ok": True, "deal_id": fill.deal_id}).encode(
                                    "utf-8"
                                )
                            )
                        else:
                            conn.sendall(b'{"ok":true,"pong":true}')
                    except Exception as exc:
                        conn.sendall(
                            json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                        )
        finally:
            try:
                server.close()
            except OSError:
                pass


def _parse_replay_timestamp(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def inject_replay_tick(
    epic: str,
    bid: float,
    offer: float,
    *,
    timestamp: float | str | None = None,
) -> None:
    """Append one replay tick to the testbed feed (harness helper)."""
    from datetime import datetime, timezone

    from system.testbed_firewall import guard_path, testbed_replay_feed_path

    path = guard_path(testbed_replay_feed_path(), operation="append")
    ts = timestamp
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    elif isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    row = {
        "type": "tick",
        "epic": str(epic),
        "bid": float(bid),
        "offer": float(offer),
        "timestamp": ts,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
