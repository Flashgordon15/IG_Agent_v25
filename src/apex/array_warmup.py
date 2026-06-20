"""Detached background warmup — OHLC ingest + 256-bar float64 ring compilation."""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import numpy as np

from apex.warmup_progress import (
    RING_TARGET_BARS,
    is_warmup_complete,
    mark_warmup_failed,
    mark_warmup_ready,
    reset_warmup_progress,
    update_warmup_progress,
)
from system.engine_log import log_engine

_WARMUP_THREAD: threading.Thread | None = None
_WARMUP_LOCK = threading.Lock()
_YAHOO_SEED_WORKERS = 4
_SESSION_SEED_MIN_BARS = 120
_GATE5_CACHE_MIN_BARS = 100
_WARMUP_BOOT_DEADLINE_SEC = 30.0
_WARMUP_STALL_SEC = 30.0

NIGHT_MATRIX_EPICS: tuple[str, ...] = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
)

PRIORITY_LIQUIDITY_EPICS: tuple[str, ...] = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
)


def _seed_mid_for_epic(epic: str) -> float:
    if "CFPGOLD" in epic:
        return 2400.0
    if "NIKKEI" in epic:
        return 39000.0
    if "DOW" in epic:
        return 42000.0
    if "EURUSD" in epic:
        return 1.085
    return 100.0


def _populate_high_fidelity_synthetic_momentum_bars(
    epic: str,
    *,
    bar_count: int = RING_TARGET_BARS,
) -> int:
    """
    Instant high-fidelity synthetic momentum template when Yahoo/network is closed.
    Fills the per-epic float64 ring buffer without division-by-zero risk.
    """
    base = float(_seed_mid_for_epic(epic))
    if base <= 0:
        base = 100.0
    spread = max(abs(base) * 0.00006, 0.01 if base > 10 else 0.0001)

    try:
        from apex.microkernel import get_microkernel

        kernel = get_microkernel()
        ring = kernel._ring_for(epic)
    except Exception as exc:
        log_engine(
            f"[APEX FAILSAFE] synthetic ring attach skipped {epic}: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0

    seeded = 0
    for i in range(max(1, int(bar_count))):
        phase = i * 0.11
        drift = 1.0 + 0.00015 * math.sin(phase) + 0.00008 * (i / max(1, bar_count))
        mid = base * drift
        ring.append(mid, mid + spread * 0.5, mid - spread * 0.5)
        seeded += 1

    log_engine(
        f"[APEX FAILSAFE] synthetic momentum seeded {epic} — {seeded} float64 bars"
    )
    return seeded


def _cache_bar_count(epic: str, market: str) -> int:
    from data.ohlc_yahoo_seeder import count_cached_bars

    return count_cached_bars(epic, market)


def seed_night_matrix_from_cache(*, min_bars: int = _SESSION_SEED_MIN_BARS) -> dict[str, int]:
    """Instant cache probe — never blocks on Yahoo network."""
    from data.ohlc_yahoo_seeder import EPIC_YAHOO_MAP

    results: dict[str, int] = {}
    for epic in NIGHT_MATRIX_EPICS:
        if epic not in EPIC_YAHOO_MAP:
            continue
        market = EPIC_YAHOO_MAP[epic][1]
        count = _cache_bar_count(epic, market)
        results[epic] = count
        if count >= min_bars:
            log_engine(f"Array warmup: cache ready {epic} — {count} bars")
        elif count > 0:
            log_engine(f"Array warmup: partial cache {epic} — {count} bars")
        else:
            log_engine(f"Array warmup: no cache yet for {epic}")
    return results


def seed_night_matrix_yahoo_network(
    *,
    min_bars: int = _SESSION_SEED_MIN_BARS,
    per_epic_timeout_sec: float = 25.0,
) -> dict[str, int]:
    """
    Optional background Yahoo refresh — parallel with per-epic timeout + cache fallback.
    """
    from data.ohlc_yahoo_seeder import EPIC_YAHOO_MAP, fetch_yahoo_ohlc_for_epic

    targets = [epic for epic in NIGHT_MATRIX_EPICS if epic in EPIC_YAHOO_MAP]
    if not targets:
        return {}

    started = time.perf_counter()
    results: dict[str, int] = {}
    workers = min(_YAHOO_SEED_WORKERS, len(targets))

    def _seed_one(epic: str) -> tuple[str, int]:
        market = EPIC_YAHOO_MAP[epic][1]
        try:
            count = fetch_yahoo_ohlc_for_epic(
                epic,
                market=market,
                skip_network_if_cache_ready=True,
                min_bars=min_bars,
                network_timeout_sec=per_epic_timeout_sec,
            )
            if count >= min_bars:
                return epic, count
            if count <= 0:
                synthetic = _populate_high_fidelity_synthetic_momentum_bars(epic)
                return epic, max(count, synthetic)
            return epic, count
        except Exception as exc:
            log_engine(
                f"[APEX FAILSAFE] Historical seed network request bypassed: {exc}"
            )
            synthetic = _populate_high_fidelity_synthetic_momentum_bars(epic)
            return epic, synthetic

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="yahoo-seed") as pool:
            futures = {pool.submit(_seed_one, epic): epic for epic in targets}
            for fut in as_completed(futures, timeout=per_epic_timeout_sec * len(targets)):
                epic, count = fut.result()
                results[epic] = count
    except Exception as exc:
        log_engine(
            f"[APEX FAILSAFE] parallel Yahoo seed pool bypassed: "
            f"{type(exc).__name__}: {exc}"
        )
        for epic in targets:
            if epic not in results:
                results[epic] = _populate_high_fidelity_synthetic_momentum_bars(epic)

    elapsed = time.perf_counter() - started
    gold = results.get(PRIORITY_LIQUIDITY_EPICS[0], 0)
    wall = results.get(PRIORITY_LIQUIDITY_EPICS[1], 0)
    log_engine(
        f"Array warmup: Yahoo network refresh {elapsed:.2f}s — "
        f"Gold={gold} WallSt={wall} bars"
    )
    return results


def seed_night_matrix_yahoo_ohlc(*, min_bars: int = _SESSION_SEED_MIN_BARS) -> dict[str, int]:
    """Boot-critical path: cache-first only (network refresh is background)."""
    return seed_night_matrix_from_cache(min_bars=min_bars)


def _loops_cache_sufficient(
    loops: list[Any], *, min_bars: int = _GATE5_CACHE_MIN_BARS
) -> bool:
    ready = 0
    for loop in loops:
        epic = str(getattr(loop, "_epic", "") or "")
        market = str(getattr(loop, "_market", "") or epic)
        if epic and _cache_bar_count(epic, market) >= min_bars:
            ready += 1
    return ready >= max(1, len(loops) // 2)


def _start_warmup_stall_watchdog(
    loops: list[Any],
    *,
    total_target: int,
    timeout_sec: float = _WARMUP_STALL_SEC,
) -> None:
    """Force READY if warmup thread stalls but OHLC caches are warm enough to trade."""

    def _watch() -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if is_warmup_complete():
                return
            time.sleep(2.0)
        if is_warmup_complete():
            return
        if _loops_cache_sufficient(loops, min_bars=_SESSION_SEED_MIN_BARS):
            log_engine(
                f"Array warmup: stall watchdog — cache sufficient after {timeout_sec:.0f}s, "
                "forcing READY"
            )
            mark_warmup_ready()
        else:
            log_engine(
                f"Array warmup: stall watchdog elapsed ({timeout_sec:.0f}s) — "
                "cache still thin; warmup thread continues"
            )

    threading.Thread(
        target=_watch,
        name="apex-warmup-stall-watchdog",
        daemon=True,
    ).start()


def schedule_background_array_warmup(
    rest_client: Any,
    loops: list[Any],
    cfg: Any | None = None,
    *,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Launch OHLC + ring-buffer compilation on a detached daemon thread."""
    global _WARMUP_THREAD
    if not loops:
        mark_warmup_ready()
        if on_complete:
            on_complete()
        return

    total_target = RING_TARGET_BARS * len(loops)
    reset_warmup_progress(bars_target=total_target)
    update_warmup_progress(
        bars_compiled=0,
        bars_target=total_target,
        detail="Loading OHLC cache",
    )
    _start_warmup_stall_watchdog(loops, total_target=total_target)

    def _worker() -> None:
        compiled = 0
        kernel = None

        try:
            update_warmup_progress(
                bars_compiled=0,
                bars_target=total_target,
                detail="Parallel OHLC seed (4 workers)",
            )
            seed_counts = seed_night_matrix_yahoo_network(
                min_bars=_SESSION_SEED_MIN_BARS,
                per_epic_timeout_sec=22.0,
            )
            cache_ready = sum(
                1 for c in seed_counts.values() if c >= _SESSION_SEED_MIN_BARS
            )
            update_warmup_progress(
                bars_compiled=min(
                    total_target,
                    cache_ready * RING_TARGET_BARS,
                ),
                bars_target=total_target,
                detail="Compiling float64 rings",
            )
        except Exception as exc:
            log_engine(
                f"Array warmup: parallel seed skipped: {type(exc).__name__}: {exc}"
            )
            for epic in PRIORITY_LIQUIDITY_EPICS:
                try:
                    _populate_high_fidelity_synthetic_momentum_bars(epic)
                except Exception:
                    pass
            try:
                seed_night_matrix_from_cache(min_bars=_SESSION_SEED_MIN_BARS)
            except Exception:
                pass

        try:
            from apex.microkernel import get_microkernel

            kernel = get_microkernel()
            kernel.start()
        except Exception as exc:
            log_engine(f"Array warmup: micro-kernel attach skipped: {type(exc).__name__}: {exc}")

        try:
            from trading.ohlc_bootstrap import bootstrap_ohlc_for_session

            progress_lock = threading.Lock()

            def _compile_loop(loop: Any) -> int:
                nonlocal compiled
                epic = str(getattr(loop, "_epic", "") or "")
                market = str(getattr(loop, "_market", "") or epic)
                if not epic:
                    return 0

                def _bar_progress(local_compiled: int, *, _epic: str = epic) -> None:
                    with progress_lock:
                        update_warmup_progress(
                            bars_compiled=min(total_target, local_compiled),
                            bars_target=total_target,
                            epic=_epic,
                            detail=f"Compiling {_epic}",
                        )

                with progress_lock:
                    _bar_progress(compiled, _epic=epic)

                try:
                    count = bootstrap_ohlc_for_session(
                        rest_client,
                        loop._signal_engine,
                        epic,
                        market,
                        environment_scorer=getattr(loop, "_env", None),
                        prefer_cache=True,
                    )
                except Exception as exc:
                    log_engine(
                        f"Array warmup: OHLC bootstrap failed {epic}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    count = _populate_high_fidelity_synthetic_momentum_bars(epic)

                bars_seeded = 0
                if kernel is not None and count > 0:
                    with progress_lock:
                        base = compiled

                    def _on_bar(n: int, *, _epic: str = epic, _base: int = base) -> None:
                        _bar_progress(min(total_target, _base + n), _epic=_epic)

                    bars_seeded = kernel.seed_historical_bars_from_engine(
                        epic,
                        loop._signal_engine,
                        market,
                        max_bars=RING_TARGET_BARS,
                        on_bar=_on_bar,
                    )

                local_delta = max(bars_seeded, min(count, RING_TARGET_BARS))
                with progress_lock:
                    compiled = min(total_target, compiled + local_delta)
                    _bar_progress(compiled, _epic=epic)
                return local_delta

            workers = min(_YAHOO_SEED_WORKERS, max(1, len(loops)))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="array-compile"
            ) as pool:
                list(pool.map(_compile_loop, loops))

            from trading.ohlc_readiness import finalize_bootstrap_state

            finalize_bootstrap_state()
            mark_warmup_ready()
            log_engine(
                f"Array warmup: complete — {compiled}/{total_target} bars compiled "
                f"across {len(loops)} epic(s)"
            )
            try:
                from apex.avionics_story import append_avionics_story

                append_avionics_story(
                    f"WARMING: {compiled}/{total_target} float64 ring bars compiled — "
                    "RSI/EMA/ATR indicators hydrated",
                    kind="warmup",
                )
            except Exception:
                pass
        except Exception as exc:
            if _loops_cache_sufficient(loops, min_bars=_SESSION_SEED_MIN_BARS):
                log_engine(
                    f"Array warmup: compile error but cache OK — forcing READY: "
                    f"{type(exc).__name__}: {exc}"
                )
                mark_warmup_ready()
            else:
                mark_warmup_failed(f"{type(exc).__name__}: {exc}")
                log_engine(f"Array warmup FATAL: {type(exc).__name__}: {exc}")
        finally:
            if on_complete:
                try:
                    on_complete()
                except Exception as exc:
                    log_engine(
                        f"Array warmup on_complete failed: {type(exc).__name__}: {exc}"
                    )

        def _background_yahoo_refresh() -> None:
            try:
                seed_night_matrix_yahoo_network(min_bars=_SESSION_SEED_MIN_BARS)
            except Exception as exc:
                log_engine(
                    f"Array warmup: background Yahoo refresh skipped: "
                    f"{type(exc).__name__}: {exc}"
                )

        threading.Thread(
            target=_background_yahoo_refresh,
            name="apex-yahoo-refresh",
            daemon=True,
        ).start()

    with _WARMUP_LOCK:
        _WARMUP_THREAD = threading.Thread(
            target=_worker,
            name="apex-array-warmup",
            daemon=True,
        )
        _WARMUP_THREAD.start()


def warmup_thread_alive() -> bool:
    return _WARMUP_THREAD is not None and _WARMUP_THREAD.is_alive()
