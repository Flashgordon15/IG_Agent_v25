"""Grok Oracle macro bias — hot-reloadable string gate (no HTTP, no bot restart).

``grok_macro_bias`` ∈ {BULL, BEAR, NEUTRAL, VETO}. Disk is peeked with a 1s
mtime cache so operators can flip stance in config without restarting. No
network I/O — safe to call from the pre-entry gate off the raw tick lane.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_VALID = frozenset({"BULL", "BEAR", "NEUTRAL", "VETO"})
_DEFAULT = "NEUTRAL"

_lock = threading.Lock()
_cache_value: str | None = None
_cache_checked_at = 0.0
_CACHE_TTL_SEC = 1.0


def reset_grok_macro_bias_cache_for_tests() -> None:
    global _cache_value, _cache_checked_at
    with _lock:
        _cache_value = None
        _cache_checked_at = 0.0


def _normalize(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    return s if s in _VALID else None


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env_cfg = (os.environ.get("IG_AGENT_CONFIG") or "").strip()
    if env_cfg:
        paths.append(Path(env_cfg))
    root = Path(__file__).resolve().parents[2]
    paths.append(root / "config" / "tuning_overlay.json")
    paths.append(root / "config" / "config_v31_demo_throughput.json")
    return paths


def _peek_from_disk() -> str | None:
    """Newest candidate file that defines ``grok_macro_bias`` wins."""
    best: str | None = None
    best_mtime = -1.0
    for path in _candidate_paths():
        try:
            if not path.is_file():
                continue
            mtime = float(path.stat().st_mtime)
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or "grok_macro_bias" not in data:
                continue
            norm = _normalize(data.get("grok_macro_bias"))
            if norm is None:
                continue
            if mtime >= best_mtime:
                best_mtime = mtime
                best = norm
        except Exception:
            continue
    return best


def resolve_grok_macro_bias(cfg: Any | None = None) -> str:
    """
    Resolution order (high-speed, no HTTP):
      1. ``IG_GROK_MACRO_BIAS`` env (ops override)
      2. Disk peek (1s cache) — enables no-restart config edits
      3. In-memory cfg
      4. NEUTRAL
    """
    global _cache_value, _cache_checked_at

    env = _normalize(os.environ.get("IG_GROK_MACRO_BIAS"))
    if env is not None:
        return env

    now = time.time()
    with _lock:
        if (now - _cache_checked_at) >= _CACHE_TTL_SEC:
            _cache_value = _peek_from_disk()
            _cache_checked_at = now
        disk = _cache_value

    if disk is not None:
        return disk

    if cfg is not None and hasattr(cfg, "get"):
        try:
            norm = _normalize(cfg.get("grok_macro_bias"))
            if norm is not None:
                return norm
        except Exception:
            pass
    return _DEFAULT


def grok_macro_blocks_entries(cfg: Any | None = None) -> tuple[bool, str]:
    """Return (blocked, reason). Only ``VETO`` fail-closes all entries."""
    bias = resolve_grok_macro_bias(cfg)
    if bias == "VETO":
        return True, "grok_macro_bias_VETO"
    return False, f"grok_macro_bias_{bias}"


def veto_lift_advisory() -> dict[str, Any]:
    """Read geopolitical cooling state — advisory only; never auto-flips bias."""
    bias = resolve_grok_macro_bias()
    cooling = False
    detail: dict[str, Any] = {}
    try:
        from alpha.geopolitical_monitor import read_macro_cooling_state, safe_for_veto_lift

        detail = read_macro_cooling_state(max_age_sec=900.0)
        cooling = bool(safe_for_veto_lift())
    except Exception as exc:
        detail = {"error": f"{type(exc).__name__}:{exc}"}
    return {
        "current_bias": bias,
        "safe_for_veto_lift": cooling,
        "suggest_neutral": bool(cooling and bias == "VETO"),
        "macro": detail,
    }
