"""Disk cache for closed-market DEMO warmup quote bars (OHLCV or synthetic)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from data.models import Quote
from system.config_loader import get_config
from system.paths import data_dir


def _cache_dir() -> Path:
    d = data_dir() / "warmup_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_date() -> str:
    return date.today().isoformat()


def _cache_path(epic: str, source: str) -> Path:
    safe_epic = epic.replace("/", "_").replace(".", "_")
    return _cache_dir() / f"{safe_epic}_{source}_{_session_date()}.json"


def market_closed_for_epic(epic: str | None) -> bool:
    if not epic:
        return False
    cfg = get_config(reload=False)
    if not cfg.market_watch_enabled:
        return False
    from system.market_watch.calendar import get_market_status

    status = get_market_status(epic)
    return status is not None and not status.open


def _quote_to_dict(q: Quote) -> dict:
    return {
        "time": q.time.isoformat(),
        "bid": q.bid,
        "offer": q.offer,
    }


def _quote_from_dict(d: dict) -> Quote | None:
    try:
        raw = str(d["time"])
        if raw.endswith("Z"):
            raw = raw[:-1]
        t = datetime.fromisoformat(raw)
        return Quote(time=t, bid=float(d["bid"]), offer=float(d["offer"]))
    except (KeyError, TypeError, ValueError):
        return None


def load_warmup_cache(epic: str, source: str) -> list[Quote] | None:
    """Return cached quotes for epic+source+today, or None if missing/invalid."""
    path = _cache_path(epic, source)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("epic") != epic or payload.get("source") != source:
            return None
        if payload.get("session_date") != _session_date():
            return None
        quotes: list[Quote] = []
        for row in payload.get("quotes") or []:
            q = _quote_from_dict(row)
            if q is not None:
                quotes.append(q)
        return quotes if len(quotes) >= 4 else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_warmup_cache(epic: str, source: str, quotes: list[Quote]) -> None:
    if len(quotes) < 4:
        return
    path = _cache_path(epic, source)
    payload = {
        "epic": epic,
        "source": source,
        "session_date": _session_date(),
        "saved_at": datetime.now().isoformat(),
        "quotes": [_quote_to_dict(q) for q in quotes],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
