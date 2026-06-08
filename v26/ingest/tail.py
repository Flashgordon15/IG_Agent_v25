"""Tail feeder JSONL for live shadow processing (rolls at UTC midnight)."""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from ingest.lake_reader import events_dir, utc_today


def tail_events(
    day: str | None = None,
    *,
    poll_sec: float = 2.0,
    follow_utc_rollover: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield feeder events from the lake file for *day*.

    When *follow_utc_rollover* is True (default), *day* is ignored after start
    and the tail follows ``utc_today()`` so v25's new daily JSONL is picked up
    at UTC midnight without restarting the shadow process.
    """
    offsets: dict[str, int] = {}
    pinned = (day or "").strip() or None

    while True:
        active_day = utc_today() if follow_utc_rollover else (pinned or utc_today())
        path = events_dir() / f"{active_day}.jsonl"
        offset = offsets.get(active_day, 0)
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                f.seek(offset)
                chunk = f.read()
                offsets[active_day] = f.tell()
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        time.sleep(poll_sec)
