"""
Global IG REST call spacing — keeps total requests under IG per-minute allowance.

All authenticated REST traffic should pass through :meth:`RestApiBudget.acquire`
(via :meth:`ig_api.rest_client.IGRestClient.request`).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from system.engine_log import log_engine

ESSENTIAL_REST_CATEGORIES = frozenset({"positions", "orders"})
# Hard cap: non-essential calls at or above this value are blocked unconditionally,
# regardless of stream state. Essential (positions/orders) and ohlc_bootstrap calls
# are exempt and do not count toward the cap. This prevents IG rate-limit hits even
# when Lightstreamer is healthy (preemptive throttle is stream-stale-only).
HARD_CAP_DEFAULT = 3
# Positions *reads* (GET) must never use priority bypass — that was the desk's
# permanent API-limit breach (false "confirm_deal" bypass storm).
_PRIORITY_BYPASS_CATEGORIES = frozenset({"orders"})
PREEMPTIVE_CONSECUTIVE_READINGS = 3
PREEMPTIVE_PAUSE_SEC = 30.0
PREEMPTIVE_PAUSE_MAX_SEC = (
    120.0  # progressive backoff ceiling when stream is persistently stale
)
PREEMPTIVE_UTILIZATION_RATIO = 0.8
FRESH_STREAM_TICK_MAX_AGE_SEC = 45.0
# Global shared-ledger soft cap for GET /positions across agent+wrappers.
POSITIONS_SHARED_MAX_PER_MINUTE = 2
# Post-boot / post-deploy: tighter cap so trade_support + OPM + sync do not storm.
POST_BOOT_POSITIONS_MAX_PER_MINUTE = 1
POST_BOOT_GRACE_SEC = 90.0

_process_boot_mono: float = time.monotonic()
_post_boot_override_until: float = 0.0
_post_boot_lock = threading.Lock()


def mark_post_boot_rest_guard(*, grace_sec: float | None = None) -> float:
    """Arm a tighter positions-poll cap for ``grace_sec`` (deploy / session_ready)."""
    global _post_boot_override_until
    sec = float(POST_BOOT_GRACE_SEC if grace_sec is None else grace_sec)
    with _post_boot_lock:
        _post_boot_override_until = time.time() + max(5.0, sec)
        return _post_boot_override_until


def post_boot_rest_guard_active() -> bool:
    """True only after ``mark_post_boot_rest_guard`` (session_ready / deploy).

    Entry pauses must not auto-fire for every young process (wrappers, pytest).
    """
    with _post_boot_lock:
        return time.time() < float(_post_boot_override_until)


def _process_youth_positions_cap() -> bool:
    """Tighten GET /positions soft-cap for the first POST_BOOT_GRACE_SEC of life."""
    return (time.monotonic() - _process_boot_mono) < float(POST_BOOT_GRACE_SEC)


def positions_poll_limit_per_min() -> float:
    """Shared GET /positions soft cap — 1/min during post-boot/youth, else 2/min."""
    if post_boot_rest_guard_active() or _process_youth_positions_cap():
        return float(POST_BOOT_POSITIONS_MAX_PER_MINUTE)
    return float(POSITIONS_SHARED_MAX_PER_MINUTE)


def priority_bypass_allowed(label: str, *, priority: bool) -> bool:
    """True only for real order-path traffic — never for GET /positions polls."""
    if not priority:
        return False
    text = str(label or "").strip()
    upper = text.upper()
    # Absolute deny: any GET positions / working-orders list poll.
    if upper.startswith("GET ") and (
        "/POSITION" in upper or "WORKINGORDER" in upper or "WORKING-ORDER" in upper
    ):
        return False
    cat = categorize_rest_label(text)
    if cat in _PRIORITY_BYPASS_CATEGORIES:
        return True
    # Explicit mutate verbs on positions (close / attach stop) may bypass.
    if any(upper.startswith(p) for p in ("POST ", "PUT ", "DELETE ")):
        if "/POSITION" in upper or "/CONFIRMS" in upper or "DEAL" in upper:
            return True
    return False


def positions_poll_deferred(*, limit_per_min: float | None = None) -> bool:
    """True when shared+local pressure says skip another GET /positions."""
    lim = float(
        positions_poll_limit_per_min() if limit_per_min is None else limit_per_min
    )
    try:
        from system import shared_rest_budget

        if shared_rest_budget.over_global_limit("ig_positions", lim):
            return True
    except Exception:
        pass
    try:
        metrics = get_rest_api_budget().metrics()
        pos_n = int((metrics.get("by_category_last_minute") or {}).get("positions") or 0)
        if pos_n >= lim:
            return True
        label = str(metrics.get("pressure_level") or "")
        # Only defer on true overrun — ELEVATED (=at warn ceiling) is allowed.
        if label in ("HIGH", "CRITICAL"):
            return True
    except Exception:
        pass
    return False


def entries_blocked_by_rest_pressure() -> tuple[bool, str]:
    """Block NEW entries when REST is ELEVATED/HIGH/CRITICAL — reserve budget for closes.

    Returns (blocked, reason). Closes / confirms keep priority bypass; this gate
    is entries-only so a 31-open storm cannot keep spending the 3/min budget.

    Do **not** gate entries on ``positions_poll_deferred()``. That helper fires at
    the designed 2/min positions soft-cap during healthy supervision and would
    permanently false-red ``trading_path_live`` while ``pressure_level`` stays OK.
    Poll coalesce remains for GET /positions deferral only.

    Demo throughput: ELEVATED (= warn ceiling) is normal Mini supervision traffic —
    only HIGH/CRITICAL pause entries. Positions coalesce storm threshold is raised
    so trade_support + OPM polling an open book does not false-red the path.

    Post-boot / cool-down: always block new entries so they never compete with
    supervise fanout or rate-limit recovery.
    """
    if post_boot_rest_guard_active():
        return True, "rest_post_boot_entry_pause"
    try:
        if get_rest_api_budget()._preemptive_pause_active():
            return True, "rest_cool_down_entry_pause"
    except Exception:
        pass
    demo = _demo_throughput_rest_bypass()
    try:
        metrics = get_rest_api_budget().metrics()
        level = str(metrics.get("pressure_level") or "IDLE").upper()
        block_levels = ("HIGH", "CRITICAL") if demo else ("ELEVATED", "HIGH", "CRITICAL")
        if level in block_levels:
            return True, f"rest_pressure_{level.lower()}"
    except Exception:
        pass
    # Real positions storm only (well over soft cap) — not the designed ceiling.
    try:
        from system import shared_rest_budget

        mult = 4.0 if demo else 2.0
        storm_lim = float(POSITIONS_SHARED_MAX_PER_MINUTE) * mult
        if shared_rest_budget.recent_count("ig_positions") > storm_lim:
            return True, "rest_positions_coalesce_pressure"
    except Exception:
        pass
    return False, ""


def pressure_level_from_counts(
    *,
    calls_last_minute: int,
    warn_per_minute: int,
    hard_cap: int,
) -> str:
    """IDLE | OK | ELEVATED | HIGH | CRITICAL — single SoT for UI.

    Essential GET /positions at the designed ceiling (warn/min) is ELEVATED,
    not HIGH — HIGH means we are *over* the advisory budget.
    """
    n = max(0, int(calls_last_minute))
    warn = max(1, int(warn_per_minute))
    hard = max(1, int(hard_cap))
    if n <= 0:
        return "IDLE"
    if n >= max(hard * 2, warn + 3):
        return "CRITICAL"
    if n > warn:
        return "HIGH"
    if n >= warn:
        return "ELEVATED"
    if n >= max(1, warn - 1):
        return "OK"
    return "OK"


def _demo_throughput_rest_bypass() -> bool:
    try:
        from system.demo_execution_plane import demo_throughput_active

        return demo_throughput_active()
    except Exception:
        return False


def _dual_core_stream_fresh(*, max_age: float = FRESH_STREAM_TICK_MAX_AGE_SEC) -> bool:
    """Stacked sweep tick pulse when hub snapshots lag behind ingest."""
    try:
        from runtime.dual_core_execution import get_active_stack_epics, get_socket_heartbeat_state
        from system.market_data_hub import get_market_data_hub

        stack = get_active_stack_epics()
        if not stack:
            return False
        hub = get_market_data_hub()
        last_ticks = (get_socket_heartbeat_state() or {}).get("last_fresh_tick_at") or {}
        now = time.time()
        for epic in stack:
            ts = last_ticks.get(epic)
            if not ts or (now - float(ts)) > max_age:
                continue
            snap = hub.get_snapshot(epic)
            if (
                snap is not None
                and float(getattr(snap, "bid", 0) or 0) > 0
                and snap.age_seconds() <= max_age
            ):
                continue
            return True
        return False
    except Exception:
        return False


class RestBudgetPausedError(RuntimeError):
    """Non-essential REST deferred — rate limit or proactive throttle."""


@dataclass(frozen=True)
class RestCallRecord:
    ts: float
    label: str
    category: str
    exempt_preemptive: bool = False


def categorize_rest_label(label: str) -> str:
    """Bucket REST paths for budget reporting."""
    s = str(label or "").upper()
    if "SESSION" in s or "LOGIN" in s or "/AUTH" in s:
        return "auth"
    if "MARKET" in s or "PRICES" in s or "SNAPSHOT" in s:
        return "market"
    if "POSITION" in s or "WORKINGORDER" in s:
        return "positions"
    if "HISTORY" in s or "TRANSACTION" in s or "ACTIVITY" in s:
        return "history"
    if "ACCOUNT" in s or "BALANCE" in s:
        return "account"
    if "CONFIRM" in s or "DEAL" in s:
        return "orders"
    return "other"


_ORDER_IN_FLIGHT_COUNT = 0
_ORDER_IN_FLIGHT_LOCK = threading.RLock()
_ORDER_IN_FLIGHT_ALLOWED_CATEGORIES = frozenset({"positions", "orders"})
_ORDER_IN_FLIGHT_PAUSED_ACTIVITIES = frozenset(
    {
        "transaction_history",
        "transaction_sync",
        "account_summary",
        "account_refresh",
        "closed_trades_refresh",
        "preview_quote",
        "verify_reconcile",
        "keepalive_sync",
        "startup_pipeline",
    }
)

_e2e_diagnostics_depth = 0
_e2e_diagnostics_lock = threading.RLock()
_ohlc_bootstrap_depth = 0
_ohlc_bootstrap_lock = threading.RLock()
_stream_quote_poll_depth = 0
_stream_quote_poll_lock = threading.RLock()


def e2e_diagnostics_rest_active() -> bool:
    """True during dashboard/CLI E2E routing checks (short diagnostic REST window)."""
    with _e2e_diagnostics_lock:
        return _e2e_diagnostics_depth > 0


def ohlc_bootstrap_rest_active() -> bool:
    """True while OHLC session bootstrap REST is in flight (excluded from preemptive budget)."""
    with _ohlc_bootstrap_lock:
        return _ohlc_bootstrap_depth > 0


@contextmanager
def ohlc_bootstrap_rest_window() -> Iterator[None]:
    """OHLC history fetches at session open must not consume the trading REST preemptive budget."""
    global _ohlc_bootstrap_depth
    with _ohlc_bootstrap_lock:
        _ohlc_bootstrap_depth += 1
    try:
        yield
    finally:
        with _ohlc_bootstrap_lock:
            _ohlc_bootstrap_depth = max(0, _ohlc_bootstrap_depth - 1)


@contextmanager
def stream_quote_poll_rest_window() -> Iterator[None]:
    """
    REST poll transport: quote fetches for subscribed epics must not consume the
  3/min hard cap — otherwise 5+ epics starve and trading loops see no quote.
    """
    global _stream_quote_poll_depth
    with _stream_quote_poll_lock:
        _stream_quote_poll_depth += 1
    try:
        yield
    finally:
        with _stream_quote_poll_lock:
            _stream_quote_poll_depth = max(0, _stream_quote_poll_depth - 1)


def stream_quote_poll_rest_active() -> bool:
    with _stream_quote_poll_lock:
        return _stream_quote_poll_depth > 0


_execution_snapshot_depth = 0
_execution_snapshot_lock = threading.RLock()


@contextmanager
def execution_snapshot_rest_window() -> Iterator[None]:
    """Gate 7 IG execution preflight — must not lose to Yahoo poll hard cap."""
    global _execution_snapshot_depth
    with _execution_snapshot_lock:
        _execution_snapshot_depth += 1
    try:
        yield
    finally:
        with _execution_snapshot_lock:
            _execution_snapshot_depth = max(0, _execution_snapshot_depth - 1)


def execution_snapshot_rest_active() -> bool:
    with _execution_snapshot_lock:
        return _execution_snapshot_depth > 0


@contextmanager
def e2e_diagnostics_rest_window() -> Iterator[None]:
    """
    Allow a few REST calls for E2E/startup diagnostics even if preemptive throttle is active.

    Does not bypass IG rate-limit backoff or order-in-flight reservation.
    """
    global _e2e_diagnostics_depth
    with _e2e_diagnostics_lock:
        _e2e_diagnostics_depth += 1
    try:
        yield
    finally:
        with _e2e_diagnostics_lock:
            _e2e_diagnostics_depth = max(0, _e2e_diagnostics_depth - 1)


def begin_order_in_flight() -> None:
    """Reserve REST budget for POST /positions/otc + GET /confirms + position sync."""
    global _ORDER_IN_FLIGHT_COUNT
    with _ORDER_IN_FLIGHT_LOCK:
        _ORDER_IN_FLIGHT_COUNT += 1
        if _ORDER_IN_FLIGHT_COUNT == 1:
            log_engine(
                "Order in flight — REST budget reserved for confirm + position sync"
            )


def end_order_in_flight() -> None:
    """Release REST reservation after terminal confirm (ACCEPTED or FAILED)."""
    global _ORDER_IN_FLIGHT_COUNT
    with _ORDER_IN_FLIGHT_LOCK:
        if _ORDER_IN_FLIGHT_COUNT <= 0:
            _ORDER_IN_FLIGHT_COUNT = 0
            return
        _ORDER_IN_FLIGHT_COUNT -= 1
        if _ORDER_IN_FLIGHT_COUNT == 0:
            log_engine("Order in flight ended — REST budget released")


def is_order_in_flight() -> bool:
    with _ORDER_IN_FLIGHT_LOCK:
        return _ORDER_IN_FLIGHT_COUNT > 0


def _primary_market_epic() -> str:
    try:
        from system.config_loader import get_config
        from trading.instrument_registry import InstrumentRegistry

        cfg = get_config(reload=False)
        reg = InstrumentRegistry(cfg.as_dict())
        enabled = reg.get_enabled()
        if enabled:
            return str(enabled[0].get("epic") or cfg.epic)
        return str(cfg.epic)
    except Exception:
        return "IX.D.NIKKEI.IFM.IP"


def hub_quote_stream_tick_age(*, epic: str | None = None) -> float | None:
    """Seconds since last valid hub bid/offer; None when missing or invalid."""
    try:
        from system.market_data_hub import get_market_data_hub

        key = str(epic or _primary_market_epic())
        snap = get_market_data_hub().get_snapshot(key)
        if snap is None or snap.bid <= 0 or snap.offer <= 0:
            return None
        return snap.age_seconds()
    except Exception:
        return None


def hub_quote_stream_genuinely_stale(
    *, max_age: float = FRESH_STREAM_TICK_MAX_AGE_SEC, epic: str | None = None
) -> bool:
    """True when the last valid quote tick is older than max_age (or absent)."""
    if _dual_core_stream_fresh(max_age=max_age):
        return False
    age = hub_quote_stream_tick_age(epic=epic)
    if age is None:
        return True
    return age > max_age


def hub_quote_stream_fresh(
    *,
    max_age: float = FRESH_STREAM_TICK_MAX_AGE_SEC,
    epic: str | None = None,
) -> bool:
    """
    True when the hub holds recent bid/offer (Lightstreamer or stream poll).

    Fresh ticks mean market data does not need REST polling — preemptive throttle
    must not block market/category REST in that window (v24 failure register #6).

    Maintenance blank-tick windows are not treated as fresh (v25: stale must be real).
    """
    try:
        from system.market_data_hub import get_market_data_hub
        from system.market_watch.japan225_session import is_quote_stream_fresh

        check_epic = str(epic or "").strip() or _primary_market_epic()
        if get_market_data_hub().is_in_maintenance(check_epic):
            return False
        if is_quote_stream_fresh(check_epic, max_age=max_age):
            return True
    except Exception:
        pass
    if _dual_core_stream_fresh(max_age=max_age):
        return True
    try:
        from system.market_data_hub import get_market_data_hub

        check_epic = str(epic or "").strip() or _primary_market_epic()
        hub = get_market_data_hub()
        with hub._lock:
            rest = hub._rest
        if rest is not None:
            activity_age = rest.stream_activity_age_seconds()
            if activity_age is not None and activity_age <= max_age:
                snap = hub.get_snapshot(check_epic)
                if snap is not None and snap.bid > 0 and snap.offer > 0:
                    return True
        with hub._lock:
            snap = hub._quotes.get(check_epic)
        if snap is not None and snap.bid > 0 and snap.offer > 0:
            return snap.age_seconds() <= max_age
        return False
    except Exception:
        return False


def _hub_in_maintenance() -> bool:
    try:
        from system.market_data_hub import get_market_data_hub

        return get_market_data_hub().is_in_maintenance(_primary_market_epic())
    except Exception:
        return False


def order_in_flight_paused(activity: str) -> bool:
    """True when non-essential REST should defer during async order confirm."""
    if not is_order_in_flight():
        return False
    act = str(activity or "").lower()
    if act in ("position_sync", "positions", "orders"):
        return False
    if act in _ORDER_IN_FLIGHT_PAUSED_ACTIVITIES:
        return True
    return bool(act) and act not in ("position_sync", "positions", "orders")


class RestApiBudget:
    """Minimum interval between REST calls (process-wide) + rolling rate metrics."""

    def __init__(
        self,
        *,
        min_interval_seconds: float = 10.0,
        warn_per_minute: int = 6,
        hard_cap_per_minute: int = HARD_CAP_DEFAULT,
    ) -> None:
        self._min_interval = max(0.5, float(min_interval_seconds))
        self._warn_per_minute = max(1, int(warn_per_minute))
        self._hard_cap = max(1, int(hard_cap_per_minute))
        self._lock = threading.RLock()
        # Initialise to now so the very first call must wait min_interval.
        # Starting at 0 caused startup bursts where all threads got through
        # simultaneously before any call was recorded.
        self._last_ts = time.time()
        self._total_waits = 0
        self._total_calls = 0
        self._recent: deque[RestCallRecord] = deque(maxlen=600)
        self._last_warn_ts = 0.0
        self._last_log_ts = 0.0
        self._preemptive_pause_until = 0.0
        self._consecutive_high_readings = 0
        self._rate_limit_skip_logged = False
        self._preemptive_skip_logged = False
        self._connecting_market_rescue_armed = False
        self._connecting_market_rescue_consumed = False

    def set_min_interval(self, seconds: float) -> None:
        with self._lock:
            self._min_interval = max(0.5, float(seconds))

    def set_warn_per_minute(self, value: int) -> None:
        with self._lock:
            self._warn_per_minute = max(1, int(value))

    def set_hard_cap_per_minute(self, value: int) -> None:
        with self._lock:
            self._hard_cap = max(1, int(value))

    def acquire(self, *, label: str = "", priority: bool = False) -> None:
        """Block until the next REST slot is available.

        ``priority=True`` may bypass min-interval / hard-cap **only** for real
        order-path calls (confirm / place / close / stop PUT). GET /positions
        polls never bypass — that false confirm_deal bypass was the permanent
        IG traffic-governor breach on the Mini desk.
        """
        from system.rate_limit_manager import get_rate_limit_manager

        cat = categorize_rest_label(label)
        allow_priority = priority_bypass_allowed(label, priority=priority)
        if not allow_priority and cat not in ESSENTIAL_REST_CATEGORIES:
            self._raise_if_non_essential_paused(cat, label=label)

        get_rate_limit_manager().check_rest_allowed()

        # Order-in-flight guard: allow confirm/position REST through immediately,
        # but defer market-data fetches while an order is being confirmed.
        # Hard 30s timeout prevents permanent deadlock when a confirm worker
        # hangs (e.g. mock orders in tests, lost IG confirm response).
        _oif_deadline = time.time() + 30.0
        while is_order_in_flight() and cat not in _ORDER_IN_FLIGHT_ALLOWED_CATEGORIES:
            if time.time() >= _oif_deadline:
                log_engine(
                    f"RestApiBudget: order-in-flight wait exceeded 30s "
                    f"(label={label!r}) — proceeding to prevent trading-loop deadlock"
                )
                break
            time.sleep(0.05)

        # Enforce minimum interval without holding the lock during sleep.
        # Reservation pattern: atomically claim the next slot, then sleep
        # outside the lock so other threads can proceed to their own slot
        # computation rather than blocking behind a sleeping holder.
        # The hard cap is also re-checked here atomically at reservation time —
        # the early check in _raise_if_non_essential_paused is approximate (reads
        # completed calls before any lock); this check is definitive and prevents
        # concurrent threads from all passing the cap simultaneously.
        _exempt_cap = (
            allow_priority
            or execution_snapshot_rest_active()
            or cat in ESSENTIAL_REST_CATEGORIES
            or e2e_diagnostics_rest_active()
            or (stream_quote_poll_rest_active() and cat == "market")
        )
        _burst_market_poll = stream_quote_poll_rest_active() and cat == "market"
        _slot_interval = 0.2 if _burst_market_poll else self._min_interval
        while True:
            with self._lock:
                now = time.time()
                elapsed = now - self._last_ts
                ready = allow_priority or elapsed >= _slot_interval
                if ready:
                    if not _exempt_cap and self._hard_cap_exceeded_locked(now):
                        raise RestBudgetPausedError("hard_rate_cap")
                    if allow_priority and elapsed < _slot_interval:
                        log_engine(
                            f"REST budget: priority bypass for order-path "
                            f"(label={label!r}, skipped {_slot_interval - elapsed:.1f}s wait)"
                        )
                    self._last_ts = now  # Reserve this slot
                    break
                wait = _slot_interval - elapsed
            self._total_waits += 1
            time.sleep(wait)

        with self._lock:
            now = time.time()
            self._total_calls += 1
            cat = categorize_rest_label(label)
            exempt_preemptive = (
                ohlc_bootstrap_rest_active()
                or stream_quote_poll_rest_active()
                or execution_snapshot_rest_active()
            )
            self._recent.append(
                RestCallRecord(now, label, cat, exempt_preemptive=exempt_preemptive)
            )
            if not exempt_preemptive:
                self._track_preemptive_locked(now)
            self._maybe_warn_locked(now)
            self._maybe_periodic_log_locked(now)

    def _rate_limit_rest_active(self) -> bool:
        from system.rate_limit_manager import get_rate_limit_manager

        return get_rate_limit_manager().is_rest_blocked()

    def _preemptive_pause_active(self) -> bool:
        return time.time() < self._preemptive_pause_until

    def _preemptive_throttle_blocks_rest(self) -> bool:
        """Preemptive pause applies only when the live stream is down/stale."""
        if _demo_throughput_rest_bypass():
            return False
        if hub_quote_stream_fresh():
            return False
        return self._preemptive_pause_active()

    def _hard_cap_calls_locked(self, now: float) -> list[RestCallRecord]:
        """Non-essential, non-ohlc-bootstrap calls in the last minute — counted toward hard cap."""
        return [
            r
            for r in self._prune_locked(now)
            if r.category not in ESSENTIAL_REST_CATEGORIES and not r.exempt_preemptive
        ]

    def _hard_cap_exceeded_locked(self, now: float) -> bool:
        return len(self._hard_cap_calls_locked(now)) >= self._hard_cap

    def _preemptive_calls_locked(self, now: float) -> list[RestCallRecord]:
        return [r for r in self._prune_locked(now) if not r.exempt_preemptive]

    def _preemptive_utilization_high_locked(self, now: float) -> bool:
        count = len(self._preemptive_calls_locked(now))
        threshold = max(
            1, math.ceil(self._warn_per_minute * PREEMPTIVE_UTILIZATION_RATIO)
        )
        return count >= threshold

    def _raise_if_non_essential_paused(self, category: str, *, label: str = "") -> None:
        if execution_snapshot_rest_active():
            return
        if category in ESSENTIAL_REST_CATEGORIES:
            return
        if e2e_diagnostics_rest_active():
            return
        if stream_quote_poll_rest_active():
            return
        if self._rate_limit_rest_active():
            if not self._rate_limit_skip_logged:
                self._rate_limit_skip_logged = True
                log_engine("Rate limit active — non-essential REST skipped")
            raise RestBudgetPausedError("rate_limit_active")
        # Hard per-minute cap: block non-essential calls unconditionally when the
        # rolling 60s count reaches the cap. Applies regardless of stream state —
        # this is the primary guard against hitting IG's API rate limit.
        # Essential (positions/orders) and ohlc_bootstrap calls are never counted
        # or blocked here, so trade execution always gets through.
        with self._lock:
            now = time.time()
            if self._hard_cap_exceeded_locked(now):
                log_engine(
                    f"REST hard cap reached ({self._hard_cap}/min) — "
                    f"non-essential call deferred (label={label!r})"
                )
                raise RestBudgetPausedError("hard_rate_cap")
        history_reconcile = bool(label) and "history/transactions" in label.lower()
        connecting_rescue = self._consume_connecting_market_rescue(label)
        if (
            self._preemptive_throttle_blocks_rest()
            and not history_reconcile
            and not connecting_rescue
        ):
            if not self._preemptive_skip_logged:
                self._preemptive_skip_logged = True
            raise RestBudgetPausedError("preemptive_throttle")

    def arm_connecting_market_rescue_once(self) -> bool:
        """Arm one preemptive-throttle bypass for the next market REST call."""
        with self._lock:
            if self._connecting_market_rescue_consumed:
                return False
            self._connecting_market_rescue_armed = True
            return True

    def _consume_connecting_market_rescue(self, label: str) -> bool:
        with self._lock:
            if not self._connecting_market_rescue_armed:
                return False
            if categorize_rest_label(label) != "market":
                return False
            self._connecting_market_rescue_armed = False
            self._connecting_market_rescue_consumed = True
            log_engine(
                "CONNECTING market rescue — preemptive throttle bypass (one-shot)"
            )
            return True

    def _track_preemptive_locked(self, now: float) -> None:
        if _hub_in_maintenance():
            return
        if _demo_throughput_rest_bypass():
            if self._preemptive_pause_active():
                self._preemptive_pause_until = 0.0
            self._consecutive_high_readings = 0
            self._stale_pause_count = 0
            return

        stream_stale = hub_quote_stream_genuinely_stale()
        budget_high = self._preemptive_utilization_high_locked(now)

        if not stream_stale:
            self._consecutive_high_readings = 0
            self._stale_pause_count = 0  # reset backoff when stream recovers
            if self._preemptive_pause_active():
                self._preemptive_pause_until = 0.0
                log_engine(
                    "REST preemptive throttle cleared — fresh Lightstreamer/stream ticks"
                )
            if not self._rate_limit_rest_active():
                self._rate_limit_skip_logged = False
                self._preemptive_skip_logged = False
            return

        if not self._preemptive_pause_active() and not self._rate_limit_rest_active():
            self._rate_limit_skip_logged = False
            self._preemptive_skip_logged = False

        if not budget_high:
            self._consecutive_high_readings = 0
            return

        self._consecutive_high_readings += 1
        if self._consecutive_high_readings >= PREEMPTIVE_CONSECUTIVE_READINGS:
            tick_age = hub_quote_stream_tick_age()
            age_s = f"{tick_age:.1f}s" if tick_age is not None else "n/a"
            # Progressive backoff: 30s → 60s → 120s as stream stays stale.
            stale_pauses = getattr(self, "_stale_pause_count", 0) + 1
            self._stale_pause_count = stale_pauses
            pause_sec = min(
                PREEMPTIVE_PAUSE_SEC * stale_pauses, PREEMPTIVE_PAUSE_MAX_SEC
            )
            self._preemptive_pause_until = now + pause_sec
            self._consecutive_high_readings = 0
            self._preemptive_skip_logged = False
            log_engine(
                f"REST approaching limit — throttling {pause_sec:.0f}s "
                f"(stream stale tick_age={age_s}, "
                f">={int(PREEMPTIVE_UTILIZATION_RATIO * 100)}% budget, "
                f"stale_count={stale_pauses})"
            )

    def _prune_locked(self, now: float) -> list[RestCallRecord]:
        cutoff = now - 60.0
        while self._recent and self._recent[0].ts < cutoff:
            self._recent.popleft()
        return list(self._recent)

    def calls_last_minute(self) -> int:
        with self._lock:
            return len(self._prune_locked(time.time()))

    def calls_in_window(
        self,
        window_sec: float,
        *,
        categories: frozenset[str] | None = None,
    ) -> int:
        with self._lock:
            now = time.time()
            cutoff = now - max(1.0, float(window_sec))
            records = [r for r in self._recent if r.ts >= cutoff]
            if categories:
                records = [r for r in records if r.category in categories]
            return len(records)

    def endpoint_counts_in_window(self, window_sec: float) -> dict[str, int]:
        with self._lock:
            now = time.time()
            cutoff = now - max(1.0, float(window_sec))
            counts: dict[str, int] = {}
            for rec in self._recent:
                if rec.ts < cutoff:
                    continue
                key = str(rec.label or rec.category or "other")
                counts[key] = counts.get(key, 0) + 1
            return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:30])

    def _maybe_warn_locked(self, now: float) -> None:
        count = len(self._prune_locked(now))
        if count < self._warn_per_minute:
            return
        if now - self._last_warn_ts < 30.0:
            return
        self._last_warn_ts = now
        by_cat = self._by_category_locked()
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items()))
        log_engine(
            f"REST budget WARN: {count} calls/min (limit advisory {self._warn_per_minute}) — {detail}"
        )

    def _maybe_periodic_log_locked(self, now: float) -> None:
        if now - self._last_log_ts < 300.0:
            return
        if self._total_calls <= 0:
            return
        self._last_log_ts = now
        count = len(self._prune_locked(now))
        by_cat = self._by_category_locked()
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())) or "none"
        log_engine(
            f"REST budget: {count}/min ({detail}) | total={self._total_calls} throttled={self._total_waits}"
        )

    def _by_category_locked(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rec in self._recent:
            out[rec.category] = out.get(rec.category, 0) + 1
        return out

    def status_label(self) -> str:
        level = self.pressure_level()
        count = self.calls_last_minute()
        if level == "IDLE":
            return "OK (idle)"
        return f"{level} ({count}/min)"

    def pressure_level(self) -> str:
        with self._lock:
            now = time.time()
            recent = self._prune_locked(now)
            return pressure_level_from_counts(
                calls_last_minute=len(recent),
                warn_per_minute=self._warn_per_minute,
                hard_cap=self._hard_cap,
            )

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            recent = self._prune_locked(now)
            by_cat = self._by_category_locked()
            hard_cap_calls = len(self._hard_cap_calls_locked(now))
            level = pressure_level_from_counts(
                calls_last_minute=len(recent),
                warn_per_minute=self._warn_per_minute,
                hard_cap=self._hard_cap,
            )
            return {
                "min_interval_sec": self._min_interval,
                "warn_per_minute": self._warn_per_minute,
                "hard_cap_per_minute": self._hard_cap,
                "hard_cap_calls_last_minute": hard_cap_calls,
                "hard_cap_utilization_pct": round(
                    hard_cap_calls / self._hard_cap * 100
                ),
                "total_calls": self._total_calls,
                "throttled_waits": self._total_waits,
                "calls_last_minute": len(recent),
                "by_category_last_minute": by_cat,
                "pressure_level": level,
                "status_label": (
                    "OK (idle)" if level == "IDLE" else f"{level} ({len(recent)}/min)"
                ),
                "last_labels": [r.label for r in recent[-5:]],
                "positions_shared_max_per_minute": positions_poll_limit_per_min(),
                "post_boot_rest_guard": post_boot_rest_guard_active(),
                "preemptive_pause_active": self._preemptive_pause_active(),
            }

    def snapshot(self) -> dict[str, Any]:
        return self.metrics()


_budget: RestApiBudget | None = None
_budget_lock = threading.Lock()


def get_rest_api_budget() -> RestApiBudget:
    global _budget
    with _budget_lock:
        if _budget is None:
            try:
                from system.config_loader import get_config

                cfg = get_config(reload=False)
                sec = float(cfg.rest_min_interval_seconds)
                warn = int(getattr(cfg, "rest_budget_warn_per_minute", 6))
            except Exception:
                sec = 10.0
                warn = 6
            _budget = RestApiBudget(min_interval_seconds=sec, warn_per_minute=warn)
        return _budget


def configure_rest_api_budget(
    *, min_interval_seconds: float | None = None
) -> RestApiBudget:
    budget = get_rest_api_budget()
    if min_interval_seconds is not None:
        budget.set_min_interval(min_interval_seconds)
        log_engine(f"REST API budget: min interval {min_interval_seconds:.1f}s")
    try:
        from system.config_loader import get_config

        cfg = get_config(reload=False)
        warn = int(getattr(cfg, "rest_budget_warn_per_minute", 6))
        budget.set_warn_per_minute(warn)
        hard_cap = int(getattr(cfg, "rest_hard_cap_per_minute", warn))
        budget.set_hard_cap_per_minute(hard_cap)
        log_engine(
            f"REST API budget: warn={warn}/min hard_cap={hard_cap}/min "
            f"interval={budget._min_interval:.0f}s"
        )
    except Exception:
        pass
    return budget


def reset_connecting_market_rescue() -> None:
    """Reset one-shot CONNECTING market rescue (new Lightstreamer connect)."""
    budget = get_rest_api_budget()
    with budget._lock:
        budget._connecting_market_rescue_armed = False
        budget._connecting_market_rescue_consumed = False


def non_essential_rest_paused() -> bool:
    """True when non-essential REST should defer (403 pause or proactive throttle)."""
    budget = get_rest_api_budget()
    return budget._rate_limit_rest_active() or budget._preemptive_throttle_blocks_rest()
