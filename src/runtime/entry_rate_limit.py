"""Per-epic entry rate limit / cooldown when approaching open caps."""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.RLock()
_last_entry_ts: dict[str, float] = {}
_hour_bucket: dict[str, list[float]] = {}
_global_bucket: list[float] = []


def _cfg_block(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return {}
    raw = cfg.get("entry_rate_limit") if hasattr(cfg, "get") else None
    return raw if isinstance(raw, dict) else {}


def _prune(stamps: list[float], now: float, window: float = 3600.0) -> list[float]:
    cut = now - window
    return [t for t in stamps if t >= cut]


def check_entry_rate_limit(
    epic: str,
    *,
    cfg: Any | None = None,
    open_count: int | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). False => block new entry for this epic."""
    block = _cfg_block(cfg)
    if not block.get("enabled", True):
        return True, "entry_rate_limit off"

    epic_s = str(epic or "").strip()
    if not epic_s:
        return True, "no epic"

    now = time.time()
    min_iv = float(block.get("per_epic_min_interval_sec") or 90)
    max_h = int(block.get("per_epic_max_per_hour") or 6)
    gmax = int(block.get("global_max_per_hour") or 12)
    approach = float(block.get("cap_approach_pct") or 0.75)
    extra = float(block.get("cap_approach_extra_cooldown_sec") or 120)

    # Cap approach: tighten interval when book is filling.
    try:
        from system.config_loader import get_config

        c = cfg or get_config()
        max_open = max(1, int(getattr(c, "max_open_positions", 6) or 6))
        n = open_count
        if n is None:
            from runtime.broker_snapshot import open_count_from_snapshot

            n = open_count_from_snapshot(max_age_sec=300.0)
        if n is not None and max_open > 0 and float(n) / float(max_open) >= approach:
            min_iv = max(min_iv, extra)
    except Exception:
        pass

    with _lock:
        last = float(_last_entry_ts.get(epic_s) or 0)
        if last > 0 and (now - last) < min_iv:
            wait = min_iv - (now - last)
            return False, f"entry_cooldown epic={epic_s} wait={wait:.0f}s"

        bucket = _prune(_hour_bucket.get(epic_s, []), now)
        if len(bucket) >= max_h:
            return False, f"entry_rate_cap epic={epic_s} {len(bucket)}/{max_h}/h"

        global_b = _prune(_global_bucket, now)
        if len(global_b) >= gmax:
            return False, f"entry_rate_global {len(global_b)}/{gmax}/h"

    return True, "ok"


def record_entry(epic: str) -> None:
    epic_s = str(epic or "").strip()
    if not epic_s:
        return
    now = time.time()
    with _lock:
        _last_entry_ts[epic_s] = now
        bucket = _prune(_hour_bucket.get(epic_s, []), now)
        bucket.append(now)
        _hour_bucket[epic_s] = bucket
        global _global_bucket
        _global_bucket = _prune(_global_bucket, now)
        _global_bucket.append(now)


def reset_for_tests() -> None:
    with _lock:
        _last_entry_ts.clear()
        _hour_bucket.clear()
        _global_bucket.clear()
