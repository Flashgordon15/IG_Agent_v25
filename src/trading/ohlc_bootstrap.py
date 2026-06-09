"""Seed SignalEngine quote history from local JSONL cache or IG REST OHLC."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.engine_log import log_engine
from system.paths import data_dir
from trading.ohlc_cache_paths import ohlc_cache_path

if TYPE_CHECKING:
    from trading.environment_scorer import EnvironmentScorer

# IG snapshotTime / snapshotTimeUTC, e.g. 2026/05/28:14:30:00 or 2026-05-28T14:30:00
_IG_SNAPSHOT_TIME = re.compile(
    r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})[T:\s](\d{1,2}):(\d{2})(?::(\d{2}))?"
)

IG_HISTORICAL_ALLOWANCE_ERROR = (
    "error.public-api.exceeded-account-historical-data-allowance"
)
_HISTORICAL_LOCKOUT_PATH = data_dir() / "state" / "ohlc_historical_lockout.json"
_HISTORICAL_LOCKOUT_TTL_SEC = 45 * 60

MIN_CACHE_BARS_FOR_BOOTSTRAP = 100

# Minimum seconds between REST OHLC fetches — keeps burst safely under 3/min cap
_OHLC_REST_STAGGER_SEC = 22.0


def _parse_bar_time(raw: str) -> datetime:
    s = str(raw or "").strip()
    if not s:
        return datetime.now()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s and "+" not in s and "Z" not in s.upper():
        head, _, tail = s.partition(".")
        if tail.isdigit() or (len(tail) >= 3 and tail[:3].isdigit()):
            s = head
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        m = _IG_SNAPSHOT_TIME.match(s)
        if not m:
            return datetime.now()
        y, mo, d, h, mi, sec = m.groups()
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def is_ig_historical_allowance_error(status_code: int, body: str | None) -> bool:
    """True when IG rejects price-history due to account historical allowance."""
    if status_code not in (403, 429):
        return False
    text = str(body or "")
    try:
        data = json.loads(text)
        if str(data.get("errorCode", "")) == IG_HISTORICAL_ALLOWANCE_ERROR:
            return True
    except (json.JSONDecodeError, TypeError):
        pass
    return IG_HISTORICAL_ALLOWANCE_ERROR in text


def mark_historical_allowance_lockout(*, source: str = "ig_rest") -> None:
    """Persist lockout so restarts stay on local cache until TTL expires."""
    try:
        _HISTORICAL_LOCKOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HISTORICAL_LOCKOUT_PATH.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "error": IG_HISTORICAL_ALLOWANCE_ERROR,
                    "source": source,
                }
            ),
            encoding="utf-8",
        )
        log_engine(
            "OHLC bootstrap: IG historical data allowance exceeded — "
            "local-cache-only mode active"
        )
    except Exception as e:
        log_engine(f"OHLC lockout state write failed: {type(e).__name__}: {e}")


def clear_historical_allowance_lockout_for_tests() -> None:
    """Reset lockout flag between pytest cases."""
    try:
        _HISTORICAL_LOCKOUT_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def is_historical_allowance_lockout() -> bool:
    """True while recent IG historical-data allowance block is in effect."""
    if not _HISTORICAL_LOCKOUT_PATH.is_file():
        return False
    try:
        raw = json.loads(_HISTORICAL_LOCKOUT_PATH.read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("ts") or 0)
        return 0 <= age < _HISTORICAL_LOCKOUT_TTL_SEC
    except Exception:
        return True


def strict_local_cache_first() -> bool:
    try:
        from system.config_loader import get_config

        return bool(getattr(get_config(), "ohlc_strict_local_cache_first", True))
    except Exception:
        return True


def local_cache_max_bars() -> int:
    try:
        from system.config_loader import get_config

        return max(
            MIN_CACHE_BARS_FOR_BOOTSTRAP,
            int(getattr(get_config(), "ohlc_local_cache_max_bars", 5000)),
        )
    except Exception:
        return 5000


def local_cache_bar_count(epic: str, market: str = "") -> int:
    path = ohlc_cache_path(epic, market=market)
    if not path.is_file():
        return 0
    try:
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                count += 1
        return count
    except OSError:
        return 0


def local_cache_ready(epic: str, market: str = "") -> bool:
    return local_cache_bar_count(epic, market) >= MIN_CACHE_BARS_FOR_BOOTSTRAP


def _bootstrap_from_cache(
    epic: str,
    market: str,
    signal_engine: SignalEngine,
    environment_scorer: Any | None,
    num_points: int,
    *,
    max_bars: int | None = None,
) -> int:
    """Seed SignalEngine from local JSONL cache when IG REST is unavailable."""
    cache_path = ohlc_cache_path(epic, market=market)
    if not cache_path.is_file():
        log_engine(f"OHLC bootstrap: no local cache at {cache_path}")
        return 0
    cap = max_bars if max_bars is not None else local_cache_max_bars()
    cap = max(cap, num_points)
    try:
        lines = [
            ln
            for ln in cache_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        if not lines:
            log_engine("OHLC bootstrap: local cache is empty")
            return 0
        tail = lines[-cap:] if len(lines) >= cap else lines
        seeded: list[Quote] = []
        for line in tail:
            bar = json.loads(line)
            high = float(bar.get("h") or 0)
            low = float(bar.get("l") or 0)
            close = float(bar.get("c") or 0)
            spread = float(bar.get("spread") or 15.0)
            if high <= 0 or low <= 0:
                continue
            bid = close - spread / 2.0
            offer = close + spread / 2.0
            seeded.append(
                Quote(
                    time=_parse_bar_time(bar.get("t", "")),
                    bid=bid,
                    offer=offer,
                )
            )
        if not seeded:
            log_engine("OHLC bootstrap: no valid bars parsed from local cache")
            return 0
        count = signal_engine.seed_ohlc_history(market, seeded, aliases=[epic])
        if count <= 0:
            log_engine("OHLC bootstrap: seed_ohlc_history rejected cache bars")
            return 0
        if environment_scorer is not None:
            environment_scorer.on_ohlc_bootstrapped(market)
        log_engine(
            f"OHLC bootstrap: injected {count} bars from local cache for {epic} "
            f"(market={market}, source={cache_path.name})"
        )
        return count
    except Exception as e:
        log_engine(f"OHLC bootstrap cache fallback failed: {type(e).__name__}: {e}")
        return 0


def bootstrap_ohlc_for_session(
    rest_client: Any,
    signal_engine: SignalEngine,
    epic: str,
    market: str,
    *,
    num_points: int = 100,
    resolution: str = "MINUTE_5",
    environment_scorer: EnvironmentScorer | None = None,
    prefer_cache: bool = True,
) -> int:
    """Inject historical bars into SignalEngine; returns count injected (0 on failure)."""
    try:
        if signal_engine is None:
            return 0

        max_cache = local_cache_max_bars()
        lockout = is_historical_allowance_lockout()
        strict = strict_local_cache_first()

        cache_first = lockout or (
            (prefer_cache or strict) and local_cache_ready(epic, market)
        )
        if cache_first:
            cached = _bootstrap_from_cache(
                epic,
                market,
                signal_engine,
                environment_scorer,
                num_points,
                max_bars=max_cache,
            )
            if cached >= MIN_CACHE_BARS_FOR_BOOTSTRAP:
                return cached
            if lockout:
                if cached > 0:
                    log_engine(
                        f"OHLC bootstrap: local cache partial ({cached} bars) — "
                        f"continuing without IG REST for {epic}"
                    )
                    return cached
                log_engine(
                    f"OHLC bootstrap: no usable local cache for {epic} "
                    f"(historical lockout — IG REST skipped)"
                )
                return 0

        if rest_client is None:
            return _bootstrap_from_cache(
                epic,
                market,
                signal_engine,
                environment_scorer,
                num_points,
                max_bars=max_cache,
            )

        fetch = getattr(rest_client, "fetch_price_history", None)
        if not callable(fetch):
            log_engine("OHLC bootstrap: fetch_price_history unavailable")
            return _bootstrap_from_cache(
                epic,
                market,
                signal_engine,
                environment_scorer,
                num_points,
                max_bars=max_cache,
            )

        from system.rest_api_budget import ohlc_bootstrap_rest_window

        with ohlc_bootstrap_rest_window():
            bars = fetch(epic, resolution=resolution, num_points=num_points)

        if not bars:
            if lockout or is_historical_allowance_lockout():
                log_engine(
                    f"OHLC bootstrap: IG historical lockout — using local cache for {epic}"
                )
            else:
                log_engine(
                    f"OHLC bootstrap: no bars from IG REST for {epic} — trying local cache"
                )
            return _bootstrap_from_cache(
                epic,
                market,
                signal_engine,
                environment_scorer,
                num_points,
                max_bars=max_cache,
            )

        seeded: list[Quote] = []
        for bar in bars:
            high = float(bar.get("high") or 0)
            low = float(bar.get("low") or 0)
            if high <= 0 or low <= 0:
                continue
            mid = (high + low) / 2.0
            bid_close = float(bar.get("bid_close") or 0)
            offer_close = float(bar.get("offer_close") or 0)
            if bid_close > 0 and offer_close > bid_close:
                spread = offer_close - bid_close
                bid = bid_close
                offer = offer_close
            else:
                spread = max(1.0, float(bar.get("close") or mid) * 0.0001)
                bid = mid - spread / 2.0
                offer = mid + spread / 2.0
            seeded.append(
                Quote(time=_parse_bar_time(bar.get("time", "")), bid=bid, offer=offer)
            )
        count = signal_engine.seed_ohlc_history(market, seeded, aliases=[epic])
        if count <= 0:
            log_engine(f"OHLC bootstrap: no valid bars for {epic}")
            return _bootstrap_from_cache(
                epic,
                market,
                signal_engine,
                environment_scorer,
                num_points,
                max_bars=max_cache,
            )
        if environment_scorer is not None:
            environment_scorer.on_ohlc_bootstrapped(market)
        log_engine(
            f"OHLC bootstrap: injected {count} bars into SignalEngine for {epic} "
            f"(market={market})"
        )
        return count
    except Exception as e:
        log_engine(f"OHLC bootstrap warning: {type(e).__name__}: {e}")
        return _bootstrap_from_cache(
            epic,
            market,
            signal_engine,
            environment_scorer,
            num_points,
            max_bars=local_cache_max_bars(),
        )


def bootstrap_ohlc_parallel(
    rest_client: Any,
    loops: list[Any],
    *,
    max_workers: int = 3,
) -> None:
    """Bootstrap OHLC for all trading loops.

    Strict local-cache-first: markets with a warm local cache are seeded from
    JSONL (up to ohlc_local_cache_max_bars) without IG REST.  When IG historical
    allowance is exceeded, all markets use local cache only — no Yahoo fallback.
    """
    if not loops:
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    open_loops: list[Any] = []
    for loop in loops:
        try:
            if loop._session.is_session_open():
                open_loops.append(loop)
        except Exception:
            open_loops.append(loop)

    if not open_loops:
        return

    cached_loops: list[Any] = []
    rest_loops: list[Any] = []
    for loop in open_loops:
        if (
            local_cache_ready(loop._epic, loop._market)
            or is_historical_allowance_lockout()
        ):
            cached_loops.append(loop)
        else:
            rest_loops.append(loop)

    def _bootstrap_one(loop: Any) -> int:
        return bootstrap_ohlc_for_session(
            rest_client,
            loop._signal_engine,
            loop._epic,
            loop._market,
            environment_scorer=loop._env,
            prefer_cache=True,
        )

    if cached_loops:
        workers = min(max(1, int(max_workers)), len(cached_loops))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_bootstrap_one, lp) for lp in cached_loops]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    log_engine(f"OHLC bootstrap error: {type(e).__name__}: {e}")

    for i, loop in enumerate(rest_loops):
        if i > 0:
            log_engine(
                f"OHLC bootstrap: staggering {_OHLC_REST_STAGGER_SEC:.0f}s before "
                f"REST fetch for {loop._epic} (rate-limit protection)"
            )
            time.sleep(_OHLC_REST_STAGGER_SEC)
        try:
            _bootstrap_one(loop)
        except Exception as e:
            log_engine(
                f"OHLC REST bootstrap error {loop._epic}: {type(e).__name__}: {e}"
            )
