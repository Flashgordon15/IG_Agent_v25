"""
Per-account REST token bucket — ADDITIONAL pacing on top of RestApiBudget.

Coexistence (do NOT remove the global 3/min non-essential hard cap):
  1. ``RestApiBudget.acquire`` — process-local spacing + 3 calls/min non-essential
  2. ``shared_rest_budget`` — cross-process advisory ledger for read lanes
  3. ``ChaosGuardian.acquire_outbound_token`` — category buckets (orders/positions)
  4. **This module** — per IG accountId wire-rate ceiling before the above run

Rates (v33 spec):
  - Z6BAH4 / QUANT_SNIPER (CFD): 40 req/s, burst capacity 40
  - Z6BAH3 / MACRO_SENTINEL (SB): 10 req/s, burst capacity 10

Order-path calls with ``budget_priority=True`` bypass this layer (same as
RestApiBudget fast-pass semantics).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from system.engine_lane import DEFAULT_ACCOUNT_CFD, DEFAULT_ACCOUNT_SB

_ACCOUNT_RATES: dict[str, tuple[float, float]] = {
    DEFAULT_ACCOUNT_CFD: (40.0, 40.0),
    DEFAULT_ACCOUNT_SB: (10.0, 10.0),
}
_DEFAULT_RATE = (20.0, 20.0)

_lock = threading.Lock()
_buckets: dict[str, "_TokenBucket"] = {}


@dataclass(slots=True)
class _TokenBucket:
    refill_per_sec: float
    capacity: float
    tokens: float
    last_refill: float


def _resolve_account_id() -> str:
    raw = (
        os.environ.get("IG_ACCOUNT_ID")
        or os.environ.get("IG_ACTIVE_ACCOUNT")
        or ""
    ).strip().upper()
    if raw:
        return raw
    try:
        from system.credentials_loader import load_credentials

        creds = load_credentials()
        acct = str(getattr(creds, "account_id", "") or "").strip().upper()
        if acct:
            return acct
    except Exception:
        pass
    return DEFAULT_ACCOUNT_CFD


def _rates_for_account(account_id: str) -> tuple[float, float]:
    key = str(account_id or "").strip().upper()
    return _ACCOUNT_RATES.get(key, _DEFAULT_RATE)


def _get_bucket(account_id: str) -> _TokenBucket:
    key = str(account_id or "").strip().upper() or "DEFAULT"
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            refill, cap = _rates_for_account(key)
            bucket = _TokenBucket(
                refill_per_sec=refill,
                capacity=cap,
                tokens=cap,
                last_refill=time.monotonic(),
            )
            _buckets[key] = bucket
        return bucket


def _refill(bucket: _TokenBucket, now: float) -> None:
    elapsed = max(0.0, now - bucket.last_refill)
    if elapsed <= 0:
        return
    bucket.tokens = min(
        bucket.capacity,
        bucket.tokens + elapsed * bucket.refill_per_sec,
    )
    bucket.last_refill = now


def acquire_account_token(
    *,
    account_id: str | None = None,
    cost: float = 1.0,
    max_wait_sec: float = 2.0,
    priority: bool = False,
) -> bool:
    """Return True when a token is consumed (or priority bypass)."""
    if priority:
        return True
    acct = str(account_id or _resolve_account_id()).strip().upper()
    bucket = _get_bucket(acct)
    deadline = time.monotonic() + max(0.0, float(max_wait_sec))
    need = max(0.0, float(cost))
    while True:
        now = time.monotonic()
        with _lock:
            _refill(bucket, now)
            if bucket.tokens >= need:
                bucket.tokens -= need
                return True
        if now >= deadline:
            return False
        time.sleep(min(0.01, deadline - now))


def snapshot() -> dict[str, Any]:
    acct = _resolve_account_id()
    bucket = _get_bucket(acct)
    with _lock:
        _refill(bucket, time.monotonic())
        refill, cap = _rates_for_account(acct)
        return {
            "account_id": acct,
            "refill_per_sec": refill,
            "capacity": cap,
            "tokens": round(bucket.tokens, 4),
            "coexists_with": [
                "RestApiBudget 3/min non-essential hard cap",
                "shared_rest_budget cross-process ledger",
                "ChaosGuardian category buckets",
            ],
        }


def reset_account_token_buckets_for_tests() -> None:
    with _lock:
        _buckets.clear()
