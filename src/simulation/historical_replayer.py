"""
Deterministic historical replay engine for HARDENED_TESTBED.

Parses CSV / Parquet / JSONL tick archives for the night-matrix epics and feeds
the MarketDataHub ingest vector with virtual-clock-aligned packets.  Supports
time-dilation via ``--speed`` / ``--dilation`` (e.g. 100x compresses 5 trading
days into minutes).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from system.engine_log import log_engine

# Night matrix — EUR/USD, Wall St, Gold, Nikkei (fourth macro index).
NIGHT_MATRIX_EPICS: frozenset[str] = frozenset(
    {
        "CS.D.EURUSD.CFD.IP",
        "IX.D.DOW.IFM.IP",
        "CS.D.CFPGOLD.CFP.IP",
        "IX.D.NIKKEI.IFM.IP",
    }
)

_EPIC_ALIASES: dict[str, str] = {
    "EURUSD": "CS.D.EURUSD.CFD.IP",
    "EUR/USD": "CS.D.EURUSD.CFD.IP",
    "DOW": "IX.D.DOW.IFM.IP",
    "WALLST": "IX.D.DOW.IFM.IP",
    "WALL ST": "IX.D.DOW.IFM.IP",
    "GOLD": "CS.D.CFPGOLD.CFP.IP",
    "XAUUSD": "CS.D.CFPGOLD.CFP.IP",
    "NIKKEI": "IX.D.NIKKEI.IFM.IP",
    "JPN225": "IX.D.NIKKEI.IFM.IP",
}


@dataclass(frozen=True)
class ReplayTick:
    epic: str
    bid: float
    offer: float
    timestamp: float


def normalize_epic(raw: str) -> str:
    key = str(raw or "").strip()
    if not key:
        return ""
    upper = key.upper()
    if key in NIGHT_MATRIX_EPICS:
        return key
    if upper in _EPIC_ALIASES:
        return _EPIC_ALIASES[upper]
    return key


def parse_timestamp(raw: Any) -> float:
    if raw is None or raw == "":
        raise ValueError("missing timestamp")
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:
            val /= 1000.0
        return val
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return float(text)
    except ValueError:
        pass
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _row_to_tick(row: dict[str, Any]) -> ReplayTick | None:
    epic = normalize_epic(
        str(row.get("epic") or row.get("instrument") or row.get("symbol") or "")
    )
    bid = float(row.get("bid") or row.get("Bid") or 0)
    offer = float(
        row.get("offer")
        or row.get("ask")
        or row.get("Offer")
        or row.get("Ask")
        or bid
    )
    ts_raw = row.get("timestamp") or row.get("ts") or row.get("time") or row.get("Time")
    if not epic or bid <= 0 or offer <= 0 or ts_raw is None:
        return None
    return ReplayTick(
        epic=epic,
        bid=bid,
        offer=offer,
        timestamp=parse_timestamp(ts_raw),
    )


def clear_replay_completion_marker() -> None:
    try:
        from system.testbed_firewall import testbed_root

        marker = testbed_root() / "replay" / ".replay_complete"
        if marker.is_file():
            marker.unlink()
    except Exception:
        pass


def _write_replay_completion_marker(emitted: int, *, speed: float = 1.0) -> None:
    try:
        from system.testbed_firewall import testbed_root

        marker = testbed_root() / "replay" / ".replay_complete"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "emitted": int(emitted),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "speed": float(speed),
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_jsonl(path: Path) -> list[ReplayTick]:
    ticks: list[ReplayTick] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row_type = str(row.get("type") or "tick")
            if row_type not in ("tick", ""):
                continue
            tick = _row_to_tick(row)
            if tick is not None:
                ticks.append(tick)
    return ticks


def load_csv(path: Path) -> list[ReplayTick]:
    ticks: list[ReplayTick] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            tick = _row_to_tick(dict(row))
            if tick is not None:
                ticks.append(tick)
    return ticks


def load_parquet(path: Path) -> list[ReplayTick]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError(
            "Parquet replay requires pandas + pyarrow — pip install pandas pyarrow"
        ) from exc
    frame = pd.read_parquet(path)
    ticks: list[ReplayTick] = []
    for row in frame.to_dict(orient="records"):
        tick = _row_to_tick(row)
        if tick is not None:
            ticks.append(tick)
    return ticks


def load_ticks(path: Path) -> list[ReplayTick]:
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson", ".json"):
        raw = load_jsonl(path)
    elif suffix == ".csv":
        raw = load_csv(path)
    elif suffix in (".parquet", ".pq"):
        raw = load_parquet(path)
    else:
        raise ValueError(f"unsupported replay format: {suffix}")
    return sorted(raw, key=lambda t: t.timestamp)


class HistoricalReplayer:
    """Merge-sorted tick playback with configurable time dilation."""

    def __init__(
        self,
        ticks: list[ReplayTick],
        *,
        speed: float = 1.0,
        hub: Any | None = None,
        feed_path: Path | None = None,
        loop: bool = False,
    ) -> None:
        self._ticks = list(ticks)
        self._speed = max(0.001, float(speed))
        self._hub = hub
        self._feed_path = feed_path
        self._loop = bool(loop)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def tick_count(self) -> int:
        return len(self._ticks)

    @property
    def speed(self) -> float:
        return self._speed

    def stop(self) -> None:
        self._stop.set()

    def _resolve_hub(self) -> Any:
        if self._hub is not None:
            return self._hub
        from system.market_data_hub import get_market_data_hub

        return get_market_data_hub()

    def emit_tick(self, tick: ReplayTick) -> None:
        hub = self._resolve_hub()
        hub.publish_replay_tick(
            tick.epic,
            tick.bid,
            tick.offer,
            quote_time=tick.timestamp,
        )
        if self._feed_path is not None:
            self._append_feed(tick)

    def _append_feed(self, tick: ReplayTick) -> None:
        row = {
            "type": "tick",
            "epic": tick.epic,
            "bid": tick.bid,
            "offer": tick.offer,
            "timestamp": datetime.fromtimestamp(
                tick.timestamp, tz=timezone.utc
            ).isoformat(),
        }
        self._feed_path.parent.mkdir(parents=True, exist_ok=True)
        with self._feed_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    async def run_async(self) -> int:
        """Replay all ticks with async sleep dilation; returns ticks emitted."""
        if not self._ticks:
            log_engine("HistoricalReplayer: no ticks to replay")
            return 0
        emitted = 0
        prev_ts: float | None = None
        while not self._stop.is_set():
            for tick in self._ticks:
                if self._stop.is_set():
                    break
                if prev_ts is not None:
                    delta = (tick.timestamp - prev_ts) / self._speed
                    if delta > 0:
                        await asyncio.sleep(delta)
                self.emit_tick(tick)
                emitted += 1
                prev_ts = tick.timestamp
            if not self._loop:
                break
            prev_ts = None
        log_engine(
            f"HistoricalReplayer: finished emitted={emitted} speed={self._speed}x"
        )
        _write_replay_completion_marker(emitted, speed=self._speed)
        return emitted

    def run_blocking(self) -> int:
        return asyncio.run(self.run_async())

    def start_background(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        def _runner() -> None:
            try:
                self.run_blocking()
            except Exception as exc:
                log_engine(
                    f"HistoricalReplayer: background error {type(exc).__name__}: {exc}"
                )
            finally:
                try:
                    from simulation.replay_clock import clear_replay_clock

                    clear_replay_clock()
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=_runner,
            name="historical-replayer",
            daemon=True,
        )
        self._thread.start()
        try:
            from simulation.replay_telemetry import register_replayer

            register_replayer(self)
        except Exception:
            pass
        return self._thread


def build_replayer(
    path: Path,
    *,
    speed: float = 1.0,
    hub: Any | None = None,
    feed_path: Path | None = None,
    loop: bool = False,
) -> HistoricalReplayer:
    ticks = load_ticks(path)
    known = [t for t in ticks if t.epic in NIGHT_MATRIX_EPICS]
    if known:
        ticks = known
    log_engine(
        f"HistoricalReplayer: loaded {len(ticks)} ticks from {path.name} "
        f"(speed={speed}x)"
    )
    return HistoricalReplayer(
        ticks,
        speed=speed,
        hub=hub,
        feed_path=feed_path,
        loop=loop,
    )


def start_background_replay(
    path: Path,
    *,
    speed: float = 10.0,
    hub: Any | None = None,
    feed_path: Path | None = None,
    loop: bool = False,
) -> HistoricalReplayer:
    replayer = build_replayer(path, speed=speed, hub=hub, feed_path=feed_path, loop=loop)
    replayer.start_background()
    try:
        from simulation.replay_telemetry import register_replayer

        register_replayer(replayer)
    except Exception:
        pass
    return replayer


def default_replay_path() -> Path:
    env = os.environ.get("IG_HISTORICAL_REPLAY", "").strip()
    if env:
        return Path(env)
    bundled = Path(__file__).resolve().parent / "data" / "production_5day_archive.jsonl"
    if bundled.is_file():
        return bundled
    return Path(__file__).resolve().parent / "data" / "sample_ticks.jsonl"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic historical tick replayer (HARDENED_TESTBED)"
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=default_replay_path(),
        help="CSV, Parquet, or JSONL tick archive",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=float(os.environ.get("IG_REPLAY_SPEED", "10")),
        help="Time-dilation multiplier (10 = 10x faster than recorded)",
    )
    parser.add_argument(
        "--dilation",
        type=float,
        default=None,
        help="Alias for --speed",
    )
    parser.add_argument(
        "--feed",
        type=Path,
        default=None,
        help="Optional testbed_replay.jsonl mirror path",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Loop the archive indefinitely",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    speed = float(args.dilation if args.dilation is not None else args.speed)
    path = args.input
    if not path.is_file():
        log_engine(f"HistoricalReplayer: input not found — {path}")
        return 2
    try:
        from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

        if get_apex_runtime_mode() is not ApexRuntimeMode.HARDENED_TESTBED:
            log_engine(
                "HistoricalReplayer: warning — not in HARDENED_TESTBED; "
                "virtual clock still active for hub ingest"
            )
    except Exception:
        pass
    feed = args.feed
    if feed is None:
        try:
            from system.testbed_firewall import is_testbed_firewall_active, testbed_replay_feed_path

            if is_testbed_firewall_active():
                feed = testbed_replay_feed_path()
        except Exception:
            feed = None
    replayer = build_replayer(path, speed=speed, feed_path=feed, loop=args.loop)
    emitted = replayer.run_blocking()
    return 0 if emitted > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
