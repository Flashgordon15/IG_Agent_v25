"""Setup-level win-rate memory from ML training store — penalise chronic losers."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system.config import Config
from system.engine_log import log_engine

_lock = threading.RLock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 120.0


@dataclass
class SetupMemoryVerdict:
    setup_key: str
    trades: int
    wins: int
    win_rate: float
    penalty_pts: float
    veto: bool
    reason: str


def _ml_learning_cfg(cfg: Config) -> dict[str, Any]:
    block = cfg.get("ml_learning")
    return dict(block) if isinstance(block, dict) else {}


def _store_path() -> Path:
    from data.ml_training_store import default_store_path

    return default_store_path()


def _load_setup_stats(setup_key: str, *, lookback: int) -> tuple[int, int]:
    path = _store_path()
    if not path.is_file():
        return 0, 0
    key = str(setup_key or "").strip()
    if not key:
        return 0, 0
    wins = 0
    total = 0
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for raw in lines[-lookback:]:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(row.get("setup_name") or row.get("setup_key") or "") != key:
                continue
            result = str(row.get("result") or "").upper()
            if result not in ("WIN", "LOSS"):
                continue
            total += 1
            if result == "WIN":
                wins += 1
    except Exception:
        return 0, 0
    return wins, total


def evaluate_setup_memory(cfg: Config, setup_key: str) -> SetupMemoryVerdict:
    """Return penalty/veto from recent ML-labelled closes for this setup."""
    ml = _ml_learning_cfg(cfg)
    if not ml.get("setup_memory_enabled", True):
        return SetupMemoryVerdict(
            setup_key=str(setup_key or ""),
            trades=0,
            wins=0,
            win_rate=0.0,
            penalty_pts=0.0,
            veto=False,
            reason="disabled",
        )

    key = str(setup_key or "").strip()
    if not key:
        return SetupMemoryVerdict(
            setup_key="",
            trades=0,
            wins=0,
            win_rate=0.0,
            penalty_pts=0.0,
            veto=False,
            reason="no_setup",
        )

    import time

    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            stats = cached[1]
            return SetupMemoryVerdict(**stats)

    lookback = int(ml.get("setup_memory_lookback", 200))
    min_trades = int(ml.get("setup_memory_min_trades", 5))
    veto_wr = float(ml.get("setup_memory_veto_wr", 0.25))
    penalty_wr = float(ml.get("setup_memory_penalty_wr", 0.40))
    penalty_pts = float(ml.get("setup_memory_penalty_pts", 12.0))

    wins, total = _load_setup_stats(key, lookback=lookback)
    wr = (wins / total) if total else 0.0
    veto = False
    penalty = 0.0
    reason = "ok"

    if total >= min_trades:
        if wr < veto_wr:
            veto = True
            reason = f"setup_wr={wr:.0%}<{veto_wr:.0%} n={total}"
            log_engine(f"[ML SETUP MEMORY] VETO {key[:40]} — {reason}")
        elif wr < penalty_wr:
            penalty = penalty_pts
            reason = f"setup_wr={wr:.0%}<{penalty_wr:.0%} n={total}"
            log_engine(f"[ML SETUP MEMORY] penalty -{penalty:.0f} {key[:40]} — {reason}")

    verdict = SetupMemoryVerdict(
        setup_key=key,
        trades=total,
        wins=wins,
        win_rate=round(wr, 4),
        penalty_pts=penalty,
        veto=veto,
        reason=reason,
    )
    with _lock:
        _cache[key] = (now, verdict.__dict__)
    return verdict


def invalidate_setup_memory_cache() -> None:
    with _lock:
        _cache.clear()
