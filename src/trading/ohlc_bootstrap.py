"""Seed SignalEngine quote history from IG REST OHLC on session open."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.engine_log import log_engine

if TYPE_CHECKING:
    from trading.environment_scorer import EnvironmentScorer

# IG snapshotTime / snapshotTimeUTC, e.g. 2026/05/28:14:30:00 or 2026-05-28T14:30:00
_IG_SNAPSHOT_TIME = re.compile(
    r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})[T:\s](\d{1,2}):(\d{2})(?::(\d{2}))?"
)


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


def _bootstrap_from_cache(
    epic: str,
    market: str,
    signal_engine: SignalEngine,
    environment_scorer: Any | None,
    num_points: int,
) -> int:
    """Seed SignalEngine from local JSONL cache when IG REST is unavailable."""
    import json

    from trading.ohlc_cache_paths import ohlc_cache_path

    cache_path = ohlc_cache_path(epic, market=market)
    if not cache_path.is_file():
        log_engine(f"OHLC bootstrap: no local cache at {cache_path}")
        return 0
    try:
        lines = cache_path.read_text(encoding="utf-8").splitlines()
        # Take the last num_points bars
        tail = lines[-num_points:] if len(lines) >= num_points else lines
        if not tail:
            log_engine("OHLC bootstrap: local cache is empty")
            return 0
        seeded: list[Quote] = []
        for line in tail:
            line = line.strip()
            if not line:
                continue
            bar = json.loads(line)
            # Cache schema: t, o, h, l, c, v, spread
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


MIN_CACHE_BARS_FOR_BOOTSTRAP = 100


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
        if prefer_cache:
            cached = _bootstrap_from_cache(
                epic, market, signal_engine, environment_scorer, num_points
            )
            if cached >= MIN_CACHE_BARS_FOR_BOOTSTRAP:
                return cached
        if rest_client is None:
            return _bootstrap_from_cache(
                epic, market, signal_engine, environment_scorer, num_points
            )
        fetch = getattr(rest_client, "fetch_price_history", None)
        if not callable(fetch):
            log_engine("OHLC bootstrap: fetch_price_history unavailable")
            return 0
        from system.rest_api_budget import ohlc_bootstrap_rest_window

        with ohlc_bootstrap_rest_window():
            bars = fetch(epic, resolution=resolution, num_points=num_points)
        if not bars:
            log_engine(
                f"OHLC bootstrap: no bars from IG REST for {epic} — trying local cache"
            )
            return _bootstrap_from_cache(
                epic, market, signal_engine, environment_scorer, num_points
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
            return 0
        if environment_scorer is not None:
            environment_scorer.on_ohlc_bootstrapped(market)
        log_engine(
            f"OHLC bootstrap: injected {count} bars into SignalEngine for {epic} "
            f"(market={market})"
        )
        return count
    except Exception as e:
        log_engine(f"OHLC bootstrap warning: {type(e).__name__}: {e}")
        return 0


# Minimum seconds between REST OHLC fetches — keeps burst safely under 3/min cap
_OHLC_REST_STAGGER_SEC = 22.0


def bootstrap_ohlc_parallel(
    rest_client: Any,
    loops: list[Any],
    *,
    max_workers: int = 3,
) -> None:
    """Bootstrap OHLC for all trading loops.

    Cache-first: markets with a warm local cache are seeded instantly without
    consuming REST budget.  Markets that need a live REST fetch run sequentially
    with a 22-second stagger to stay safely under the 3-calls/min hard cap.
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

    # Split into cache-serviced vs REST-needed based on local cache presence
    cached_loops: list[Any] = []
    rest_loops: list[Any] = []
    for loop in open_loops:
        try:
            from system.paths import data_dir

            slug = loop._epic.replace(".", "_").replace("/", "_")
            cache_path = data_dir() / "ohlc_cache" / f"{slug}_5m.jsonl"
            if cache_path.exists() and cache_path.stat().st_size > 1024:
                cached_loops.append(loop)
            else:
                rest_loops.append(loop)
        except Exception:
            rest_loops.append(loop)

    def _bootstrap_one(loop: Any) -> int:
        if rest_client is None:
            return 0
        return bootstrap_ohlc_for_session(
            rest_client,
            loop._signal_engine,
            loop._epic,
            loop._market,
            environment_scorer=loop._env,
            prefer_cache=True,
        )

    # Cached markets: parallel, no REST calls, no rate-limit risk
    if cached_loops:
        workers = min(max(1, int(max_workers)), len(cached_loops))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_bootstrap_one, lp) for lp in cached_loops]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    log_engine(f"OHLC bootstrap error: {type(e).__name__}: {e}")

    # REST-needed markets: sequential with stagger to avoid rate-limit burst
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
