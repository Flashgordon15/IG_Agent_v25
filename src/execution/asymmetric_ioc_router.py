"""Native IG OTC MARKET routing with asymmetric maxSlippage.

IG Spread/CFD `/positions/otc` does not use exchange FILL_OR_KILL / timeInForce.
Entries are MARKET with gateway `maxSlippage` (fraction × current spread, integer
points) so IG rejects mid-transit drift instead of filling into adverse friction.

Hot-path guards:
  - fail-closed if AuthManager tokens invalid or refresh/reauth in flight
  - 60s veto after 5 slippage/pricing rejections in a rolling 30s window

Fill-rate telemetry (async) may relax slip multiplier 0.5→1.0 when rolling
20-trade fill rate < 40% AND OBI is exceptionally strong (|OBI| ≥ 0.40).
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from collections import deque
from typing import Any

from system.engine_log import log_engine

# Consecutive rejection back-off (module state; reset in tests)
_REJECT_WINDOW_SEC = 30.0
_REJECT_THRESHOLD = 5
_BACKOFF_SEC = 60.0
_reject_times: deque[float] = deque()
_consecutive_rejects = 0
_backoff_until = 0.0
_state_lock = threading.Lock()

# Rolling closed-trade WR → fractional expectancy sizing (last N outcomes)
_EXPECTANCY_WINDOW = 10
_EXPECTANCY_FLOOR_MULT = 0.35
_EXPECTANCY_NEUTRAL_WR = 0.50
_closed_outcomes: deque[float] = deque(maxlen=_EXPECTANCY_WINDOW)

# TWAP shard wheel — variable window scales with clip count
_TWAP_BASE_WINDOW_SEC = 2.5
_TWAP_MAX_CLIPS = 5
_TWAP_MIN_CLIP_INTERVAL_SEC = 0.05

_SLIPPAGE_REJECT_MARKERS = (
    "slippage",
    "maxslippage",
    "amendment_95",
    "attouch",
    "marketclosed",
    "market not open",
    "pricing",
    "price unavailable",
    "level unavailable",
    "spread",
    "reject",
    "rejected",
)


class AsymmetricIOCRouter:
    """Router facade — ``order_in_flight`` mirrors the per-account mutex gate."""

    def __init__(self) -> None:
        self.order_in_flight: bool = False

    def sync_from_mutex(self, account_id: str | None = None) -> None:
        try:
            from execution.order_in_flight_mutex import get_order_mutex

            locked = get_order_mutex().is_locked(account_id)
            self.order_in_flight = bool(locked or get_order_mutex().order_in_flight)
        except Exception:
            pass


_ROUTER = AsymmetricIOCRouter()


def get_asymmetric_ioc_router() -> AsymmetricIOCRouter:
    return _ROUTER


def reset_asymmetric_router_state_for_tests() -> None:
    """Clear rejection / back-off / WR-contraction state (unit tests only)."""
    global _consecutive_rejects, _backoff_until
    with _state_lock:
        _reject_times.clear()
        _consecutive_rejects = 0
        _backoff_until = 0.0
        _closed_outcomes.clear()
    _ROUTER.order_in_flight = False
    try:
        from execution.order_in_flight_mutex import reset_order_mutex_for_tests

        reset_order_mutex_for_tests()
    except Exception:
        pass
    try:
        from diagnostics.fill_rate_monitor import reset_fill_rate_monitor_for_tests

        reset_fill_rate_monitor_for_tests()
    except Exception:
        pass


def note_closed_trade_outcome(pnl_gbp: float) -> None:
    """Record a closed-trade PnL for rolling fractional expectancy sizing."""
    with _state_lock:
        _closed_outcomes.append(float(pnl_gbp))


def rolling_win_rate() -> float | None:
    with _state_lock:
        outcomes = list(_closed_outcomes)
    if not outcomes:
        return None
    wins = sum(1 for p in outcomes if p > 0)
    return wins / float(len(outcomes))


def fractional_expectancy_multiplier() -> float:
    """
    Smooth size curve from rolling 10-trade win record.

    WR ≥ 50% → 1.0×; WR → 0% → ``_EXPECTANCY_FLOOR_MULT`` (0.35×).
    """
    wr = rolling_win_rate()
    if wr is None or len(_closed_outcomes) < 3:
        return 1.0
    if wr >= _EXPECTANCY_NEUTRAL_WR:
        return 1.0
    span = _EXPECTANCY_NEUTRAL_WR
    t = wr / span
    return _EXPECTANCY_FLOOR_MULT + t * (1.0 - _EXPECTANCY_FLOOR_MULT)


def size_contraction_multiplier() -> float:
    """Backward-compatible alias — fractional expectancy curve."""
    return fractional_expectancy_multiplier()


def apply_wr_size_contraction(
    size: float,
    *,
    epic: str | None = None,
    cfg: Any | None = None,
) -> float:
    """Scale transmit size by fractional expectancy curve; floor at IG min lot."""
    contracted = float(size) * fractional_expectancy_multiplier()
    if epic:
        floor = _twap_min_lot(str(epic), cfg)
        if floor > 0:
            contracted = max(floor, contracted)
    return contracted


def _resolve_min_lot(epic: str, cfg: Any | None) -> float:
    try:
        from execution.size_floors import hard_min_deal_size

        return float(hard_min_deal_size(str(epic), cfg=cfg) or 0.0)
    except Exception:
        return 0.0


def _twap_min_lot(epic: str, cfg: Any | None) -> float:
    """
    Spreadbet operational min for TWAP sharding — avoids micro-lot 0.1 probe
    splitting standard 0.5 £/pt desk tickets.
    """
    key = str(epic or "").strip()
    try:
        from execution.size_floors import (
            SPREADBET_DEFAULT_MIN_DEAL,
            SPREADBET_MIN_DEAL_BY_EPIC,
            _SPREADBET_PATTERN_FLOORS,
            _pattern_floor,
        )

        if key in SPREADBET_MIN_DEAL_BY_EPIC:
            return float(SPREADBET_MIN_DEAL_BY_EPIC[key])
        pat = _pattern_floor(key, _SPREADBET_PATTERN_FLOORS)
        if pat > 0:
            return pat
        return float(SPREADBET_DEFAULT_MIN_DEAL)
    except Exception:
        return _resolve_min_lot(epic, cfg)


def twap_variable_window_sec(clip_count: int) -> float:
    """Variable TWAP wheel duration — wider books get slightly longer windows."""
    n = max(1, int(clip_count))
    return min(12.0, _TWAP_BASE_WINDOW_SEC + 0.45 * (n - 1))


def plan_twap_fragments(
    size: float,
    *,
    epic: str,
    cfg: Any | None = None,
    max_clips: int = _TWAP_MAX_CLIPS,
    account_id: str | None = None,
) -> list[float]:
    """
    Shard size into ≥min-lot clips when total exceeds one contract.

    Returns a single-element list when fragmentation is unnecessary.
    Hard-capped accounts (Z6BAH4) NEVER fragment — each clip is forceOpen
    and would cascade past hard_cap=1.
    """
    total = float(size)
    if total <= 0:
        return [total]
    try:
        from execution.order_in_flight_mutex import resolve_account_hard_open_cap

        acct = str(account_id or "").strip().upper()
        if not acct:
            acct = str(os.environ.get("IG_ACCOUNT_ID") or "").strip().upper()
        if resolve_account_hard_open_cap(acct) is not None:
            return [total]
    except Exception:
        pass
    min_lot = _twap_min_lot(str(epic), cfg)
    if min_lot <= 0 or total <= min_lot * 1.001:
        return [total]

    max_clips_eff = max(2, min(int(max_clips), int(math.floor(total / min_lot))))
    raw_clip = total / float(max_clips_eff)
    clip = max(min_lot, raw_clip)
    fragments: list[float] = []
    remaining = total
    while remaining > min_lot * 1.001 and len(fragments) < max_clips_eff:
        take = min(clip, remaining)
        fragments.append(round(take, 4))
        remaining = round(remaining - take, 4)
    if remaining > 1e-9:
        if fragments:
            fragments[-1] = round(fragments[-1] + remaining, 4)
        else:
            fragments.append(round(remaining, 4))
    if not fragments:
        return [total]
    drift = round(total - sum(fragments), 4)
    if abs(drift) > 1e-9:
        fragments[-1] = round(fragments[-1] + drift, 4)
    return fragments


async def dispatch_asymmetric_ioc_twap_async(
    rest: Any,
    *,
    epic: str,
    direction: str,
    size: float,
    bid: float,
    offer: float,
    stop_distance: float,
    limit_distance: float | None = None,
    currency_code: str = "GBP",
    cfg: Any | None = None,
    obi: float | None = None,
    twap_window_sec: float | None = None,
) -> dict[str, Any]:
    """Async TWAP wheel — each clip re-checks ``CORE_DETACHED`` before IG dispatch."""
    from execution.maintenance_detachment import is_core_detached, suppress_order_dispatch

    if is_core_detached():
        return suppress_order_dispatch(
            source="asymmetric_ioc_router_twap",
            epic=str(epic),
            direction=str(direction),
            action="entry",
            orderType="MARKET",
            size=float(size),
        )

    sized = apply_wr_size_contraction(float(size), epic=str(epic), cfg=cfg)
    fragments = plan_twap_fragments(
        sized,
        epic=str(epic),
        cfg=cfg,
        account_id=str(getattr(rest, "account_id", "") or ""),
    )
    if len(fragments) <= 1:
        return dispatch_asymmetric_ioc_limit(
            rest,
            epic=epic,
            direction=direction,
            size=float(sized),
            bid=bid,
            offer=offer,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            currency_code=currency_code,
            cfg=cfg,
            obi=obi,
        )

    window = float(twap_window_sec or twap_variable_window_sec(len(fragments)))
    interval = max(_TWAP_MIN_CLIP_INTERVAL_SEC, window / float(len(fragments)))
    log_engine(
        f"AsymmetricIOC TWAP: {len(fragments)} clips over {window:.2f}s "
        f"epic={epic} total={sized:g}"
    )

    clip_results: list[dict[str, Any]] = []
    for idx, clip in enumerate(fragments):
        if is_core_detached():
            pkt = suppress_order_dispatch(
                source="asymmetric_ioc_router_twap_clip",
                epic=str(epic),
                direction=str(direction),
                action="entry",
                orderType="MARKET",
                size=float(clip),
                twap_clip_index=idx,
                twap_clip_count=len(fragments),
            )
            clip_results.append(pkt)
            break
        if idx > 0:
            await asyncio.sleep(interval)
        clip_results.append(
            dispatch_asymmetric_ioc_limit(
                rest,
                epic=epic,
                direction=direction,
                size=float(clip),
                bid=bid,
                offer=offer,
                stop_distance=stop_distance,
                limit_distance=limit_distance,
                currency_code=currency_code,
                cfg=cfg,
                obi=obi,
            )
        )

    last = clip_results[-1] if clip_results else {}
    return {
        **last,
        "twap": True,
        "twap_clip_count": len(fragments),
        "twap_window_sec": round(window, 3),
        "twap_fragments": fragments,
        "twap_results": clip_results,
    }


def touch_level(direction: str, bid: float, offer: float) -> float:
    """Aggressive inside reference: buy the ask, sell the bid (diagnostics / gates)."""
    d = str(direction or "").upper()
    if d == "BUY":
        return float(offer)
    return float(bid)


def compute_max_slippage(
    bid: float,
    offer: float,
    *,
    slip_mult: float = 0.5,
    epic: str | None = None,
) -> int | float:
    """
    Dynamic maxSlippage = slip_mult × current IG spread.

    Indices/commodities: integer points (IG OTC index ticketing).
    Forex (``is_forex`` asset profile): fractional pips so IG does not reject
    decimal FX contracts when whole-integer index slippage is sent.
    """
    spread = float(offer) - float(bid)
    if spread <= 0:
        return 1
    mult = float(slip_mult)
    if mult <= 0:
        mult = 0.5
    raw = spread * mult

    try:
        from execution.contract_asset_normalizer import get_contract_asset_normalizer

        return get_contract_asset_normalizer().compute_max_slippage(
            epic, bid, offer, slip_mult=mult
        )
    except Exception:
        pass

    return max(1, int(round(raw)))


def adapt_max_slippage_for_ig(
    epic: str,
    max_slippage: int | float,
) -> int | float:
    """Normalize maxSlippage for IG OTC — forex keeps fractional pips."""
    try:
        from execution.contract_asset_normalizer import get_contract_asset_normalizer

        return get_contract_asset_normalizer().adapt_max_slippage(epic, max_slippage)
    except Exception:
        pass
    return max(1, int(round(float(max_slippage))))


def resolve_slip_multiplier(
    *,
    direction: str,
    obi: float | None = None,
) -> float:
    """
    Base 0.5x; expand to monitor's cached multiplier (up to 1.0x) only when
    rolling fill-rate is stressed AND OBI is exceptionally strong.
    """
    try:
        from diagnostics.fill_rate_monitor import (
            BASE_SLIP_MULT,
            get_fill_rate_monitor,
            is_high_conviction_obi,
        )

        if not is_high_conviction_obi(direction, obi):
            return BASE_SLIP_MULT
        mon = get_fill_rate_monitor()
        mult = float(mon.current_slip_multiplier())
        return max(BASE_SLIP_MULT, min(1.0, mult))
    except Exception:
        return 0.5


def build_ig_otc_market_payload(
    *,
    epic: str,
    direction: str,
    size: float,
    stop_distance: float,
    max_slippage: int | float,
    limit_distance: float | None = None,
    currency_code: str = "GBP",
) -> dict[str, Any]:
    """Native IG `/positions/otc` MARKET body — no exchange TIF keys."""
    slip = adapt_max_slippage_for_ig(str(epic), max_slippage)
    payload: dict[str, Any] = {
        "epic": str(epic),
        "expiry": "-",
        "direction": str(direction or "BUY").upper(),
        "size": float(size),
        "orderType": "MARKET",
        "maxSlippage": slip,
        "guaranteedStop": False,
        "forceOpen": True,
        "currencyCode": str(currency_code or "GBP"),
        "stopDistance": float(stop_distance),
    }
    if limit_distance is not None and float(limit_distance) > 0:
        payload["limitDistance"] = float(limit_distance)
    return payload


def asymmetric_ioc_enabled(cfg: Any | None = None) -> bool:
    if cfg is None:
        return True
    try:
        block = cfg.get("asymmetric_ioc_routing") if hasattr(cfg, "get") else None
        if isinstance(block, dict):
            return bool(block.get("enabled", True))
    except Exception:
        pass
    return True


def _backoff_active(now: float | None = None) -> bool:
    t = time.time() if now is None else float(now)
    with _state_lock:
        return t < _backoff_until


def _backoff_remaining_sec(now: float | None = None) -> float:
    t = time.time() if now is None else float(now)
    with _state_lock:
        return max(0.0, _backoff_until - t)


def _telemetry():
    try:
        from diagnostics.fill_rate_monitor import get_fill_rate_monitor

        return get_fill_rate_monitor()
    except Exception:
        return None


def _note_order_success() -> None:
    global _consecutive_rejects
    with _state_lock:
        _consecutive_rejects = 0
        _reject_times.clear()
    mon = _telemetry()
    if mon is not None:
        mon.record_fill()


def _note_slippage_rejection(now: float | None = None, detail: str = "") -> None:
    """Record a slippage/pricing reject; arm 60s back-off at 5-in-30s."""
    global _consecutive_rejects, _backoff_until
    t = time.time() if now is None else float(now)
    armed = False
    with _state_lock:
        _reject_times.append(t)
        while _reject_times and (t - _reject_times[0]) > _REJECT_WINDOW_SEC:
            _reject_times.popleft()
        _consecutive_rejects += 1
        in_window = len(_reject_times)
        if _consecutive_rejects >= _REJECT_THRESHOLD and in_window >= _REJECT_THRESHOLD:
            _backoff_until = t + _BACKOFF_SEC
            armed = True
            log_engine(
                f"AsymmetricIOC: BACKOFF 60s after {_consecutive_rejects} "
                f"slippage/pricing rejects in {_REJECT_WINDOW_SEC:.0f}s"
            )
    mon = _telemetry()
    if mon is not None:
        mon.record_slippage_reject(detail)
        if armed:
            mon.notify_backoff_activated()


def _is_slippage_or_pricing_reject(exc_or_body: Any) -> bool:
    text = str(exc_or_body or "").lower()
    if not text:
        return False
    return any(m in text for m in _SLIPPAGE_REJECT_MARKERS)


def auth_lane_ready(rest: Any) -> tuple[bool, str]:
    """
    Fail-closed hot-path auth check — never triggers login/refresh.

    Ready only when tokens are valid and no refresh / eviction handshake is active.
    """
    ready_fn = getattr(rest, "auth_ready_for_hot_path", None)
    if callable(ready_fn):
        try:
            if bool(ready_fn()):
                return True, ""
            return False, "auth_lane_not_ready"
        except Exception as exc:
            return False, f"auth_probe_error:{type(exc).__name__}"

    # Fallback: inspect AuthManager / refresh flags without calling network paths
    if bool(getattr(rest, "_session_refresh_in_progress", False)):
        return False, "session_refresh_in_progress"
    if bool(getattr(rest, "_token_eviction_in_progress", False)):
        return False, "token_eviction_in_progress"

    auth = getattr(rest, "_auth", None)
    tokens = getattr(auth, "tokens", None) if auth is not None else getattr(rest, "session", None)
    if tokens is None:
        return False, "tokens_missing"
    try:
        # Prefer private SessionTokens on AuthManager
        if auth is not None and hasattr(auth, "_tokens"):
            tok = getattr(auth, "_tokens", None)
            if tok is None or not bool(getattr(tok, "is_valid", False)):
                return False, "tokens_invalid"
            return True, ""
        if not bool(getattr(tokens, "is_valid", False)):
            return False, "tokens_invalid"
        return True, ""
    except Exception as exc:
        return False, f"auth_inspect_error:{type(exc).__name__}"


def _veto(reason: str, **extra: Any) -> dict[str, Any]:
    log_engine(f"AsymmetricIOC: VETO {reason}")
    if "auth" in str(reason).lower() or reason in (
        "auth_lane_not_ready",
        "tokens_invalid",
        "tokens_missing",
        "session_refresh_in_progress",
        "token_eviction_in_progress",
    ):
        mon = _telemetry()
        if mon is not None:
            mon.record_auth_veto(reason)
    out: dict[str, Any] = {
        "vetoed": True,
        "reason": reason,
        "dealReference": None,
        "orderType": "MARKET",
    }
    out.update(extra)
    return out


def dispatch_asymmetric_ioc_limit(
    rest: Any,
    *,
    epic: str,
    direction: str,
    size: float,
    bid: float,
    offer: float,
    stop_distance: float,
    limit_distance: float | None = None,
    currency_code: str = "GBP",
    cfg: Any | None = None,
    obi: float | None = None,
) -> dict[str, Any]:
    """
    Dispatch native IG OTC MARKET with dynamic maxSlippage.

    Name retained for call-site compatibility; payload is MARKET (not LIMIT/FOK).
    Optional ``obi`` enables high-conviction slip relaxation when fill-rate < 40%.
    """
    from execution.maintenance_detachment import is_core_detached, suppress_order_dispatch
    from execution.order_in_flight_mutex import (
        hard_cap_blocks_entry,
        mutex_veto_payload,
        release_order_mutex,
        try_acquire_order_mutex,
    )

    if is_core_detached():
        return suppress_order_dispatch(
            source="asymmetric_ioc_router",
            epic=str(epic),
            direction=str(direction),
            action="entry",
            orderType="MARKET",
            size=float(size),
        )

    _ = cfg  # reserved for future routing knobs
    dir_u = str(direction or "BUY").upper()
    if bid <= 0 or offer <= bid:
        return _veto("invalid_touch_book", bid=bid, offer=offer)

    if _backoff_active():
        return _veto(
            "rejection_backoff",
            backoff_remaining_sec=round(_backoff_remaining_sec(), 1),
        )

    ready, auth_reason = auth_lane_ready(rest)
    if not ready:
        return _veto(auth_reason or "auth_lane_not_ready")

    account_id = str(
        getattr(rest, "account_id", "") or ""
    ).strip().upper()

    cap_blocked, cap_reason = hard_cap_blocks_entry(account_id, rest=rest)
    if cap_blocked:
        return _veto(cap_reason or "account_hard_cap")

    if not try_acquire_order_mutex(
        account_id, epic=str(epic), source="asymmetric_ioc_router"
    ):
        return mutex_veto_payload(
            account_id=account_id, source="asymmetric_ioc_router"
        )
    _ROUTER.order_in_flight = True
    _ROUTER.sync_from_mutex(account_id)

    terminal = False
    filled = False
    try:
        sized = apply_wr_size_contraction(float(size), epic=str(epic), cfg=cfg)
        if sized + 1e-12 < float(size):
            log_engine(
                f"AsymmetricIOC: expectancy size scale {float(size):g}→{sized:g} "
                f"epic={epic} mult={fractional_expectancy_multiplier():.2f}"
            )

        fragments = plan_twap_fragments(
            sized,
            epic=str(epic),
            cfg=cfg,
            account_id=account_id,
        )
        if len(fragments) > 1:
            window = twap_variable_window_sec(len(fragments))
            interval = max(
                _TWAP_MIN_CLIP_INTERVAL_SEC, window / float(len(fragments))
            )
            log_engine(
                f"AsymmetricIOC TWAP sync: {len(fragments)} clips over {window:.2f}s "
                f"epic={epic} total={sized:g}"
            )
            clip_results: list[dict[str, Any]] = []
            for idx, clip in enumerate(fragments):
                if is_core_detached():
                    terminal = True
                    return suppress_order_dispatch(
                        source="asymmetric_ioc_router_twap_clip",
                        epic=str(epic),
                        direction=str(direction),
                        action="entry",
                        orderType="MARKET",
                        size=float(clip),
                        twap_clip_index=idx,
                        twap_clip_count=len(fragments),
                    )
                if idx > 0:
                    time.sleep(interval)
                clip_results.append(
                    _dispatch_asymmetric_ioc_single_clip(
                        rest,
                        epic=epic,
                        direction=dir_u,
                        size=float(clip),
                        bid=bid,
                        offer=offer,
                        stop_distance=stop_distance,
                        limit_distance=limit_distance,
                        currency_code=currency_code,
                        cfg=cfg,
                        obi=obi,
                    )
                )
            last = clip_results[-1] if clip_results else {}
            terminal = True
            if any(
                isinstance(r, dict) and r.get("dealReference") for r in clip_results
            ):
                filled = True
            return {
                **last,
                "twap": True,
                "twap_clip_count": len(fragments),
                "twap_window_sec": round(window, 3),
                "twap_fragments": fragments,
                "twap_results": clip_results,
            }

        terminal = True
        result = _dispatch_asymmetric_ioc_single_clip(
            rest,
            epic=epic,
            direction=dir_u,
            size=float(sized),
            bid=bid,
            offer=offer,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            currency_code=currency_code,
            cfg=cfg,
            obi=obi,
        )
        if isinstance(result, dict):
            # Hard-cap: any non-reject broker response keeps the ledger slot —
            # missing dealReference must NOT roll back (cascade vector).
            from execution.order_in_flight_mutex import resolve_account_hard_open_cap

            status = str(result.get("status") or result.get("dealStatus") or "").upper()
            rejected = bool(result.get("vetoed")) or status in (
                "REJECTED",
                "ERROR",
                "FAILED",
            )
            if result.get("dealReference") or (
                resolve_account_hard_open_cap(account_id) is not None and not rejected
            ):
                filled = True
        return result
    except (ConnectionError, TimeoutError, BrokenPipeError, OSError):
        # Ambiguous after dispatch — leave mutex; orchestrator clears after 5s.
        terminal = False
        raise
    except Exception:
        # Clear reject / local failure — release so the book is not wedged.
        # Hard-cap: still treat as ambiguous (do not roll back ledger) if the
        # exception may have occurred after POST left the wire.
        from execution.order_in_flight_mutex import resolve_account_hard_open_cap

        if resolve_account_hard_open_cap(account_id) is not None:
            terminal = False  # leave mutex + reservation for reconciler
        else:
            terminal = True
        raise
    finally:
        if terminal:
            release_order_mutex(
                account_id, reason="broker_confirm_or_reject", filled=filled
            )
            if filled:
                try:
                    from execution.order_in_flight_mutex import (
                        note_account_open,
                        resolve_account_hard_open_cap,
                    )

                    # Hard-capped accounts reserved the ledger slot at acquire.
                    if resolve_account_hard_open_cap(account_id) is None:
                        note_account_open(account_id, delta=1)
                except Exception:
                    pass
            _ROUTER.order_in_flight = False
            _ROUTER.sync_from_mutex(account_id)
        else:
            _ROUTER.sync_from_mutex(account_id)


def _dispatch_asymmetric_ioc_single_clip(
    rest: Any,
    *,
    epic: str,
    direction: str,
    size: float,
    bid: float,
    offer: float,
    stop_distance: float,
    limit_distance: float | None = None,
    currency_code: str = "GBP",
    cfg: Any | None = None,
    obi: float | None = None,
) -> dict[str, Any]:
    """Single MARKET clip — caller must have passed CORE_DETACHED gate."""
    dir_u = str(direction or "BUY").upper()
    mon = _telemetry()
    if mon is not None:
        mon.record_attempt()

    slip_mult = resolve_slip_multiplier(direction=dir_u, obi=obi)
    max_slip = compute_max_slippage(
        bid, offer, slip_mult=slip_mult, epic=str(epic)
    )
    try:
        from system.memory_context import get_runtime_context

        get_runtime_context().set_active_epic(str(epic))
    except Exception:
        pass
    level = touch_level(dir_u, bid, offer)
    from execution.live_broker_order_router import normalize_placement_distances

    stop_n, limit_n, stop_res = normalize_placement_distances(
        rest,
        str(epic),
        stop_distance=float(stop_distance),
        limit_distance=limit_distance,
    )
    if stop_n > float(stop_distance) + 1e-9:
        log_engine(
            f"AsymmetricIOC: floored stop epic={epic} "
            f"{float(stop_distance):g}→{stop_n:g} pts "
            f"(broker min={stop_res.min_points:g})"
        )
    payload = build_ig_otc_market_payload(
        epic=epic,
        direction=dir_u,
        size=float(size),
        stop_distance=float(stop_n),
        max_slippage=max_slip,
        limit_distance=limit_n,
        currency_code=currency_code,
    )

    log_engine(
        f"AsymmetricIOC: MARKET {dir_u} epic={epic} maxSlippage={max_slip} "
        f"slipMult={slip_mult:.1f}x size={size} touch={level:.2f} "
        f"stop={stop_n:g} limit={limit_n if limit_n is not None else '-'} "
        f"(bid={bid:.2f} ask={offer:.2f})"
    )

    try:
        from system.forensic_network_log import log_forensic_network

        account_id = str(getattr(rest, "account_id", "") or "")
        log_forensic_network(
            account_id=account_id,
            method="POST",
            path="/positions/otc",
            request_json=payload,
            source="asymmetric_ioc_router",
            phase="pre_dispatch",
            extra={
                "epic": epic,
                "direction": dir_u,
                "size": float(size),
                "maxSlippage": max_slip,
                "slip_multiplier": slip_mult,
                "touch_level": level,
            },
        )
    except Exception:
        pass

    def _reraise_placement(exc: BaseException) -> None:
        from execution.instrument_suspension import maybe_raise_from_error
        from ig_api.exceptions import InstrumentSuspendedException

        if isinstance(exc, InstrumentSuspendedException):
            raise exc
        maybe_raise_from_error(exc, epic=epic)
        if _is_slippage_or_pricing_reject(exc):
            _note_slippage_rejection(detail=str(exc))
        elif mon is not None:
            mon.record_other_fail(str(exc))
        raise

    if hasattr(rest, "place_otc_market_payload"):
        try:
            result = rest.place_otc_market_payload(payload)
            _note_order_success()
            if isinstance(result, dict):
                result.setdefault("maxSlippage", max_slip)
                result.setdefault("orderType", "MARKET")
                result.setdefault("slip_multiplier", slip_mult)
            return result
        except Exception as exc:
            _reraise_placement(exc)

    if hasattr(rest, "place_market_order"):
        try:
            result = rest.place_market_order(
                epic=epic,
                direction=dir_u,
                size=float(size),
                stop_distance=float(stop_n),
                limit_distance=limit_n,
                currency_code=currency_code,
                max_slippage=max_slip,
                force_market=True,
            )
            _note_order_success()
            if isinstance(result, dict):
                result.setdefault("maxSlippage", max_slip)
                result.setdefault("orderType", "MARKET")
                result.setdefault("ig_payload", payload)
                result.setdefault("slip_multiplier", slip_mult)
            return result
        except TypeError:
            try:
                result = rest.place_market_order(
                    epic=epic,
                    direction=dir_u,
                    size=float(size),
                    stop_distance=float(stop_n),
                    limit_distance=limit_n,
                    currency_code=currency_code,
                )
                _note_order_success()
                return result
            except Exception as exc:
                _reraise_placement(exc)
        except Exception as exc:
            _reraise_placement(exc)

    raise RuntimeError("rest client cannot place IG OTC MARKET entry")
