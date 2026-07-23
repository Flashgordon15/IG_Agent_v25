"""Weekly performance ledger — async PERF worker, journal-backed metrics.

Reads ``metrics/daily_journal.csv`` via ``performance_journal.journal_path()``.
Air-gapped from the tick lane: a daemon worker refreshes an in-memory cache;
``compile_weekly_metrics()`` never blocks hot-path callers on disk I/O when
cache is warm.
"""

from __future__ import annotations

import csv
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from system.engine_lane import (
    DEFAULT_ACCOUNT_CFD,
    DEFAULT_ACCOUNT_SB,
    DEFAULT_PRODUCT_CFD,
    DEFAULT_PRODUCT_SB,
    ENGINE_ORIGIN_CFD,
    ENGINE_ORIGIN_SB,
    infer_engine_id,
    resolve_journal_metadata,
)
from system.engine_log import log_engine

_REFRESH_SEC = float(os.environ.get("IG_WEEKLY_LEDGER_REFRESH_SEC", "45"))
_WEEK_DAYS = 7

_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_started = False
_sync_mode = False
_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


def reset_weekly_performance_ledger_for_tests() -> None:
    """Clear worker + cache (unit tests only)."""
    global _started, _thread, _sync_mode
    stop_weekly_performance_ledger()
    _sync_mode = False
    with _lock:
        _cache["ts"] = 0.0
        _cache["payload"] = None


def enable_weekly_ledger_sync_mode_for_tests(enabled: bool = True) -> None:
    global _sync_mode
    _sync_mode = bool(enabled)


def _journal_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    from diagnostics.performance_journal import journal_path

    return journal_path()


def _parse_ts_day(ts: str) -> str:
    raw = str(ts or "").strip()
    if len(raw) >= 10:
        return raw[:10]
    return ""


def _is_skippable_row(deal_id: str) -> bool:
    deal = str(deal_id or "")
    return deal.startswith("FLAT_SESSION") or deal.startswith("BENCHMARK_OFFSET")


def _read_weekly_rows(*, path: Path | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Settled journal rows within the rolling 7-day UTC window."""
    from diagnostics.performance_journal import _asset_label, _learning_deal_epic_map

    p = _journal_path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return []

    anchor = now or datetime.now(tz=timezone.utc)
    week_start = (anchor - timedelta(days=_WEEK_DAYS - 1)).strftime("%Y-%m-%d")
    week_end = anchor.strftime("%Y-%m-%d")
    deal_map = _learning_deal_epic_map()
    rows: list[dict[str, Any]] = []

    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                deal = str(row.get("DealID") or "")
                if _is_skippable_row(deal):
                    continue
                day = _parse_ts_day(str(row.get("Timestamp") or ""))
                if not day or day < week_start or day > week_end:
                    continue
                try:
                    pnl = float(row.get("RealizedPnL_GBP") or 0)
                except (TypeError, ValueError):
                    continue
                direction = str(row.get("Direction") or "").strip().upper()
                entry_s = str(row.get("EntryPrice") or "").strip()
                exit_s = str(row.get("ExitPrice") or "").strip()
                try:
                    entry_f = float(entry_s) if entry_s else None
                    exit_f = float(exit_s) if exit_s else None
                except (TypeError, ValueError):
                    entry_f, exit_f = None, None
                if (
                    entry_f is not None
                    and exit_f is not None
                    and abs(entry_f - exit_f) < 1e-9
                    and abs(pnl) < 1e-9
                ):
                    continue

                account_id = str(row.get("AccountID") or "").strip().upper()
                product_type = str(row.get("ProductType") or "").strip().upper()
                engine_origin = str(row.get("EngineOrigin") or "").strip().upper()
                if not account_id:
                    meta = resolve_journal_metadata(
                        product_type=product_type or None,
                        engine_origin=engine_origin or None,
                    )
                    account_id = str(meta.get("account_id") or "").strip().upper()
                    product_type = str(meta.get("product_type") or product_type).strip().upper()
                    engine_origin = str(meta.get("engine_origin") or engine_origin).strip().upper()
                if not account_id:
                    account_id = DEFAULT_ACCOUNT_SB
                if not product_type:
                    product_type = (
                        DEFAULT_PRODUCT_CFD
                        if account_id == DEFAULT_ACCOUNT_CFD
                        else DEFAULT_PRODUCT_SB
                    )
                if not engine_origin:
                    engine_origin = (
                        ENGINE_ORIGIN_CFD
                        if account_id == DEFAULT_ACCOUNT_CFD
                        else ENGINE_ORIGIN_SB
                    )

                epic = deal_map.get(deal, "")
                rows.append(
                    {
                        "timestamp": str(row.get("Timestamp") or ""),
                        "day": day,
                        "deal_id": deal,
                        "direction": direction or "—",
                        "net_pnl_gbp": round(pnl, 4),
                        "asset": _asset_label(epic, None, deal),
                        "account_id": account_id,
                        "product_type": product_type,
                        "engine_origin": engine_origin,
                        "engine_id": infer_engine_id(
                            account_id=account_id,
                            product_type=product_type,
                        ),
                    }
                )
    except OSError:
        return []
    return rows


def _compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from system.pnl_math import classify_result_gbp

    pnls: list[float] = []
    wins_n = 0
    losses_n = 0
    gross_wins = 0.0
    gross_losses = 0.0
    by_day: dict[str, float] = {}
    by_asset: dict[str, dict[str, Any]] = {}

    for r in rows:
        try:
            pnl = float(r.get("net_pnl_gbp") or 0)
        except (TypeError, ValueError):
            continue
        if classify_result_gbp(pnl) == "BREAKEVEN":
            continue
        pnls.append(pnl)
        day = str(r.get("day") or "")
        if day:
            by_day[day] = round(by_day.get(day, 0.0) + pnl, 4)
        asset = str(r.get("asset") or "—")
        slot = by_asset.setdefault(
            asset,
            {"asset": asset, "pnl_gbp": 0.0, "trades": 0, "wins": 0, "losses": 0},
        )
        slot["trades"] = int(slot["trades"]) + 1
        slot["pnl_gbp"] = round(float(slot["pnl_gbp"]) + pnl, 4)
        if pnl > 0:
            wins_n += 1
            gross_wins += pnl
            slot["wins"] = int(slot["wins"]) + 1
        elif pnl < 0:
            losses_n += 1
            gross_losses += abs(pnl)
            slot["losses"] = int(slot["losses"]) + 1

    n = len(pnls)
    if gross_losses > 1e-12:
        asymmetric_pf = gross_wins / gross_losses
    elif gross_wins > 0:
        asymmetric_pf = 99.0
    else:
        asymmetric_pf = 0.0

    win_rate = (wins_n / n) if n else 0.0
    weekly_sharpe: float | None = None
    daily_returns = list(by_day.values())
    if len(daily_returns) >= 2:
        mean = sum(daily_returns) / len(daily_returns)
        var = sum((p - mean) ** 2 for p in daily_returns) / max(1, len(daily_returns) - 1)
        std = math.sqrt(max(0.0, var))
        if std > 1e-12:
            weekly_sharpe = round((mean / std) * math.sqrt(min(len(daily_returns), 252)), 4)
        else:
            weekly_sharpe = 0.0
    elif len(daily_returns) == 1:
        weekly_sharpe = 0.0

    asset_breakdown: list[dict[str, Any]] = []
    for asset, slot in sorted(by_asset.items(), key=lambda kv: abs(float(kv[1]["pnl_gbp"])), reverse=True):
        trades = int(slot["trades"])
        w = int(slot["wins"])
        l = int(slot["losses"])
        asset_breakdown.append(
            {
                "asset": asset,
                "pnl_gbp": round(float(slot["pnl_gbp"]), 4),
                "trades": trades,
                "wins": w,
                "losses": l,
                "win_rate": round(w / trades, 4) if trades else 0.0,
            }
        )

    return {
        "weekly_sharpe": weekly_sharpe,
        "asymmetric_profit_factor": round(float(asymmetric_pf), 4),
        "win_rate": round(float(win_rate), 4),
        "wins": int(wins_n),
        "losses": int(losses_n),
        "gross_wins_gbp": round(float(gross_wins), 4),
        "gross_losses_gbp": round(float(gross_losses), 4),
        "net_pnl_gbp": round(float(gross_wins - gross_losses), 4),
        "sample_n": int(n),
        "trading_days": len(by_day),
        "asset_breakdown": asset_breakdown,
    }


def _empty_account_block(account_id: str) -> dict[str, Any]:
    product = DEFAULT_PRODUCT_CFD if account_id == DEFAULT_ACCOUNT_CFD else DEFAULT_PRODUCT_SB
    origin = ENGINE_ORIGIN_CFD if account_id == DEFAULT_ACCOUNT_CFD else ENGINE_ORIGIN_SB
    base = _compute_metrics([])
    return {
        "account_id": account_id,
        "product_type": product,
        "engine_origin": origin,
        **base,
    }


def _build_payload(*, path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    anchor = now or datetime.now(tz=timezone.utc)
    week_end = anchor.strftime("%Y-%m-%d")
    week_start = (anchor - timedelta(days=_WEEK_DAYS - 1)).strftime("%Y-%m-%d")
    rows = _read_weekly_rows(path=path, now=anchor)

    by_account: dict[str, list[dict[str, Any]]] = {
        DEFAULT_ACCOUNT_CFD: [],
        DEFAULT_ACCOUNT_SB: [],
    }
    other: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        acct = str(r.get("account_id") or DEFAULT_ACCOUNT_SB).strip().upper()
        if acct in by_account:
            by_account[acct].append(r)
        else:
            other.setdefault(acct, []).append(r)

    accounts: dict[str, Any] = {}
    for acct in (DEFAULT_ACCOUNT_CFD, DEFAULT_ACCOUNT_SB):
        block = _compute_metrics(by_account[acct])
        sample = by_account[acct][0] if by_account[acct] else None
        accounts[acct] = {
            "account_id": acct,
            "product_type": str((sample or {}).get("product_type") or _empty_account_block(acct)["product_type"]),
            "engine_origin": str((sample or {}).get("engine_origin") or _empty_account_block(acct)["engine_origin"]),
            **block,
        }
    for acct, acct_rows in other.items():
        block = _compute_metrics(acct_rows)
        sample = acct_rows[0]
        accounts[acct] = {
            "account_id": acct,
            "product_type": str(sample.get("product_type") or ""),
            "engine_origin": str(sample.get("engine_origin") or ""),
            **block,
        }

    merged = _compute_metrics(rows)
    return {
        "ok": True,
        "source": "journal_csv",
        "journal": str(_journal_path(path)),
        "week_start": week_start,
        "week_end": week_end,
        "week_days": _WEEK_DAYS,
        "merged": merged,
        "accounts": accounts,
        "asset_breakdown": merged.get("asset_breakdown") or [],
        "cache_age_sec": 0.0,
    }


class WeeklyPerformanceLedger:
    """Rolling 7-day desk ledger — win rate, asymmetric PF, Sharpe, assets."""

    @classmethod
    def compile_weekly_metrics(
        cls,
        *,
        force_refresh: bool = False,
        path: Path | None = None,
    ) -> dict[str, Any]:
        """Return cached weekly metrics when warm; recompute on miss or force."""
        now = time.time()
        with _lock:
            cached = _cache.get("payload")
            age = now - float(_cache.get("ts") or 0.0)
            if (
                not force_refresh
                and isinstance(cached, dict)
                and age < _REFRESH_SEC
                and path is None
            ):
                out = dict(cached)
                out["cache_age_sec"] = round(age, 2)
                return out

        payload = _build_payload(path=path)
        with _lock:
            if path is None:
                _cache["ts"] = now
                _cache["payload"] = dict(payload)
        return dict(payload)


def _refresh_loop() -> None:
    while not _stop.is_set():
        try:
            WeeklyPerformanceLedger.compile_weekly_metrics(force_refresh=True)
        except Exception as exc:
            log_engine(f"weekly_performance_ledger: refresh failed {type(exc).__name__}: {exc}")
        _stop.wait(max(10.0, float(_REFRESH_SEC)))


def start_weekly_performance_ledger() -> None:
    """Start PERF background cache refresh. Idempotent."""
    global _thread, _started
    if _sync_mode or os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_refresh_loop,
            name="perf-weekly-ledger",
            daemon=True,
        )
        _thread.start()
        _started = True
    log_engine(
        f"weekly_performance_ledger: started (refresh {_REFRESH_SEC:.0f}s, window {_WEEK_DAYS}d)"
    )


def stop_weekly_performance_ledger() -> None:
    global _started, _thread
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    with _lock:
        _started = False
        _thread = None
