"""Tail feeder JSONL for live shadow processing."""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from ingest.lake_reader import events_dir


def tail_events(day: str, *, poll_sec: float = 2.0) -> Iterator[dict[str, Any]]:
    path = events_dir() / f"{day}.jsonl"
    offset = 0
    while True:
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        time.sleep(poll_sec)
