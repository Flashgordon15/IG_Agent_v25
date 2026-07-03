"""Live calendar blackout — high-impact events ±30m (config + Finnhub)."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from system.paths import project_root


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _calendar_config() -> dict[str, Any]:
    path = project_root() / "config" / "calendar.json"
    if not path.is_file():
        return {"block_minutes_before": 30, "block_minutes_after": 30, "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {"block_minutes_before": 30, "block_minutes_after": 30, "events": []}


def _finnhub_events() -> list[dict[str, Any]]:
    path = project_root() / "data_lake" / "external" / "finnhub_economic_calendar.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data.get("events") if isinstance(data, dict) else []
        return events if isinstance(events, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _epic_in_markets(epic: str, markets: Any) -> bool:
    if not markets:
        return True
    if isinstance(markets, str):
        return epic == markets or not markets
    if isinstance(markets, list):
        return epic in markets or len(markets) == 0
    return True


def is_calendar_blocked(
    epic: str,
    *,
    at: datetime | None = None,
) -> tuple[bool, str]:
    """Return (blocked, reason) for epic at UTC time."""
    dt = at or datetime.now(timezone.utc)
    cfg = _calendar_config()
    before = int(cfg.get("block_minutes_before") or 30)
    after = int(cfg.get("block_minutes_after") or 30)
    try:
        from system.v26_config import calendar_block_minutes

        v26_before, v26_after = calendar_block_minutes()
        if v26_before > 0:
            before = v26_before
        if v26_after > 0:
            after = v26_after
    except Exception:
        pass
    window_before = timedelta(minutes=before)
    window_after = timedelta(minutes=after)

    for ev in _finnhub_events():
        if not isinstance(ev, dict):
            continue
        impact = str(ev.get("impact") or "").lower()
        if impact and impact not in ("high", "3"):
            continue
        ev_dt = _parse_ts(str(ev.get("time") or ""))
        if ev_dt is None:
            continue
        if not _epic_in_markets(epic, ev.get("markets")):
            continue
        if (ev_dt - window_before) <= dt <= (ev_dt + window_after):
            title = str(ev.get("title") or "high-impact event")[:80]
            return True, f"calendar ±{before}m: {title}"

    for ev in cfg.get("events") or []:
        if not isinstance(ev, dict):
            continue
        impact = str(ev.get("impact") or "").lower()
        if impact and impact not in ("high", "3"):
            continue
        ev_dt = _parse_ts(str(ev.get("time") or ""))
        if ev_dt is None:
            continue
        if not _epic_in_markets(epic, ev.get("markets")):
            continue
        if (ev_dt - window_before) <= dt <= (ev_dt + window_after):
            title = str(ev.get("title") or "high-impact event")[:80]
            return True, f"calendar ±{before}m: {title}"

    return False, ""


def reset_calendar_gate_cache_for_tests() -> None:
    _calendar_config.cache_clear()
    reset_news_proximity_cache_for_tests()


# ---------------------------------------------------------------------------
# News-flow volatility vectorizer (active ML feature — not passive block only)
# ---------------------------------------------------------------------------

_NEWS_CACHE: dict[str, dict[str, Any]] = {}
_NEWS_CACHE_LOCK = threading.Lock()
_NEWS_CACHE_TTL_SEC = 30.0


def reset_news_proximity_cache_for_tests() -> None:
    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE.clear()


def _iter_high_impact_events(epic: str) -> list[tuple[datetime, str, str]]:
    """Yield (event_dt, title, impact) for epic-relevant high-impact events."""
    out: list[tuple[datetime, str, str]] = []
    for ev in _finnhub_events():
        if not isinstance(ev, dict):
            continue
        impact = str(ev.get("impact") or "").lower()
        if impact and impact not in ("high", "3"):
            continue
        ev_dt = _parse_ts(str(ev.get("time") or ""))
        if ev_dt is None:
            continue
        if not _epic_in_markets(epic, ev.get("markets")):
            continue
        title = str(ev.get("title") or "high-impact event")[:80]
        out.append((ev_dt, title, impact or "high"))
    cfg = _calendar_config()
    for ev in cfg.get("events") or []:
        if not isinstance(ev, dict):
            continue
        impact = str(ev.get("impact") or "").lower()
        if impact and impact not in ("high", "3"):
            continue
        ev_dt = _parse_ts(str(ev.get("time") or ""))
        if ev_dt is None:
            continue
        if not _epic_in_markets(epic, ev.get("markets")):
            continue
        title = str(ev.get("title") or "high-impact event")[:80]
        out.append((ev_dt, title, impact or "high"))
    return out


def next_high_impact_event(
    epic: str,
    *,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Nearest future high-impact event for *epic*."""
    dt = at or datetime.now(timezone.utc)
    best: dict[str, Any] | None = None
    best_sec = float("inf")
    for ev_dt, title, impact in _iter_high_impact_events(epic):
        delta = (ev_dt - dt).total_seconds()
        if delta < -60.0:
            continue
        if delta < best_sec:
            best_sec = delta
            best = {
                "title": title,
                "impact": impact,
                "event_at": ev_dt.isoformat(),
                "seconds_until": max(0.0, delta),
            }
    return best


def news_proximity_features(
    epic: str,
    *,
    at: datetime | None = None,
    use_cache: bool = True,
) -> dict[str, float]:
    """
    Rolling T-minus countdown vector for ML ingestion.

    Returns seconds_to_next, countdown_norm (1=imminent), news_velocity, in_block_window.
    """
    key = str(epic or "").strip()
    if not key:
        return {
            "seconds_to_next": 86400.0,
            "countdown_norm": 0.0,
            "news_velocity": 0.0,
            "in_block_window": 0.0,
            "trailing_sensitivity_scale": 1.0,
        }
    now_ts = time.time()
    if use_cache:
        with _NEWS_CACHE_LOCK:
            cached = _NEWS_CACHE.get(key)
            if cached and (now_ts - float(cached.get("_ts") or 0)) < _NEWS_CACHE_TTL_SEC:
                return {k: float(v) for k, v in cached.items() if not k.startswith("_")}

    dt = at or datetime.now(timezone.utc)
    blocked, _reason = is_calendar_blocked(key, at=dt)
    nxt = next_high_impact_event(key, at=dt)
    seconds = float(nxt.get("seconds_until", 86400.0)) if nxt else 86400.0
    # Quantize: full urgency inside 15m, decay to 0 at 4h
    horizon = 14400.0
    countdown_norm = float(max(0.0, min(1.0, 1.0 - seconds / horizon)))
    if seconds <= 900.0:
        countdown_norm = float(max(countdown_norm, 1.0 - seconds / 900.0))
    news_velocity = float(countdown_norm ** 1.35)
    trailing_scale = 1.0 + news_velocity * 0.85

    payload = {
        "seconds_to_next": seconds,
        "countdown_norm": round(countdown_norm, 4),
        "news_velocity": round(news_velocity, 4),
        "in_block_window": 1.0 if blocked else 0.0,
        "trailing_sensitivity_scale": round(trailing_scale, 4),
        "_ts": now_ts,
    }
    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE[key] = payload
    return {k: float(v) for k, v in payload.items() if not k.startswith("_")}


def quantize_news_countdown_vector(epic: str, *, dims: int = 8) -> list[float]:
    """Quantized T-minus buckets (seconds) for feature-state embedding."""
    feats = news_proximity_features(epic)
    seconds = float(feats.get("seconds_to_next") or 86400.0)
    buckets = (300.0, 900.0, 1800.0, 3600.0, 7200.0, 14400.0, 28800.0, 86400.0)
    vec: list[float] = []
    for i, edge in enumerate(buckets[: max(1, dims)]):
        if i == 0:
            vec.append(1.0 if seconds <= edge else 0.0)
        else:
            prev = buckets[i - 1]
            vec.append(1.0 if prev < seconds <= edge else 0.0)
    while len(vec) < dims:
        vec.append(0.0)
    return vec[:dims]
