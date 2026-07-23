"""In-memory IG spread baseline (1-hour MA) — no I/O, O(1) hot-path updates."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# 1 sample/sec × 3600s ≈ 1h window
_MAX_SAMPLES = 3600
_MIN_SAMPLES_FOR_MA = 30
_DEFAULT_ELASTICITY = 1.5

_lock = threading.Lock()
_books: dict[str, deque[tuple[float, float, float, float]]] = {}
# deque entries: (ts, spread, bid, offer)


@dataclass(frozen=True)
class SpreadElasticityState:
    epic: str
    spread: float
    spread_ma: float
    ratio: float
    elastic: bool
    sample_count: int
    historical_bid: float
    historical_offer: float


def reset_spread_elasticity_for_tests() -> None:
    with _lock:
        _books.clear()


def observe_spread(epic: str, bid: float, offer: float, *, now: float | None = None) -> None:
    """Record a streaming spread sample (throttled to ≤1/sec per epic)."""
    key = str(epic or "").strip()
    b = float(bid or 0)
    o = float(offer or 0)
    if not key or b <= 0 or o <= b:
        return
    t = time.time() if now is None else float(now)
    spread = o - b
    with _lock:
        q = _books.get(key)
        if q is None:
            q = deque(maxlen=_MAX_SAMPLES)
            _books[key] = q
        if q and (t - q[-1][0]) < 1.0:
            # Refresh last sample in-place (same second)
            q[-1] = (t, spread, b, o)
        else:
            q.append((t, spread, b, o))
        # Drop samples older than 1 hour
        cutoff = t - 3600.0
        while q and q[0][0] < cutoff:
            q.popleft()


def spread_elasticity_state(
    epic: str,
    bid: float,
    offer: float,
    *,
    elasticity_mult: float = _DEFAULT_ELASTICITY,
) -> SpreadElasticityState:
    """Compute current vs 1h MA; ``elastic=True`` when spread > mult × MA."""
    key = str(epic or "").strip()
    b = float(bid or 0)
    o = float(offer or 0)
    spread = max(0.0, o - b) if b > 0 and o > b else 0.0
    with _lock:
        q = _books.get(key)
        if not q or len(q) < _MIN_SAMPLES_FOR_MA:
            return SpreadElasticityState(
                epic=key,
                spread=spread,
                spread_ma=spread,
                ratio=1.0,
                elastic=False,
                sample_count=len(q) if q else 0,
                historical_bid=b,
                historical_offer=o,
            )
        spreads = [s[1] for s in q]
        bids = [s[2] for s in q]
        offers = [s[3] for s in q]
        ma = sum(spreads) / len(spreads)
        hb = sum(bids) / len(bids)
        ho = sum(offers) / len(offers)
        n = len(q)
    ratio = (spread / ma) if ma > 1e-12 else 1.0
    mult = float(elasticity_mult) if elasticity_mult > 0 else _DEFAULT_ELASTICITY
    return SpreadElasticityState(
        epic=key,
        spread=spread,
        spread_ma=ma,
        ratio=ratio,
        elastic=bool(ma > 0 and spread > mult * ma),
        sample_count=n,
        historical_bid=hb,
        historical_offer=ho,
    )


def historical_inside_touch(direction: str, state: SpreadElasticityState) -> float:
    """Passive touch for mean-reversion WO: BUY@hist bid, SELL@hist offer."""
    d = str(direction or "BUY").upper()
    if d == "BUY":
        return float(state.historical_bid)
    return float(state.historical_offer)


def elasticity_cfg(cfg: Any | None) -> dict[str, Any]:
    if cfg is None or not hasattr(cfg, "get"):
        return {}
    try:
        block = cfg.get("spread_elasticity") or {}
        return dict(block) if isinstance(block, dict) else {}
    except Exception:
        return {}
