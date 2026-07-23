"""Non-blocking performance journal for the £1,000 daily milestone.

Hot-path callers only ``queue.put_nowait`` a close/flat event. A daemon worker
appends one CSV line to ``metrics/daily_journal.csv``:

    Timestamp,DealID,Direction,EntryPrice,ExitPrice,RealizedPnL_GBP,
    ClosingFillRate,ActiveSlipMultiplier,AccountID,ProductType,EngineOrigin

Hooks ``fill_rate_monitor`` for ClosingFillRate / ActiveSlipMultiplier at
write time — never on the Lightstreamer lane.
"""

from __future__ import annotations

import csv
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_HEADER = [
    "Timestamp",
    "DealID",
    "Direction",
    "EntryPrice",
    "ExitPrice",
    "RealizedPnL_GBP",
    "ClosingFillRate",
    "ActiveSlipMultiplier",
    "AccountID",
    "ProductType",
    "EngineOrigin",
    "ExitReason",
    "HoldSec",
    "Style",
]

# Hold ≥ 3m (long_trade_runner arm window) tags SB/CFD closes as long.
_LONG_HOLD_SEC = 180.0


def infer_trade_style(
    *,
    engine_origin: str = "",
    exit_reason: str = "",
    hold_sec: float | None = None,
    style: str | None = None,
) -> str:
    """Resolve ``scalp|long|macro|supervised_exit|unknown`` for journal / ML."""
    explicit = str(style or "").strip().lower()
    if explicit in ("scalp", "long", "macro", "supervised_exit"):
        return explicit
    origin = str(engine_origin or "").upper()
    reason = str(exit_reason or "").lower()
    if (
        "LONG" in origin
        or "long_runner" in reason
        or "long_trade" in reason
        or "runner_extended" in reason
    ):
        return "long"
    if hold_sec is not None:
        try:
            if float(hold_sec) >= _LONG_HOLD_SEC:
                return "long"
            if float(hold_sec) >= 0:
                # Short supervised holds default scalp unless macro origin.
                if "SENTINEL" in origin or "MACRO" in origin:
                    return "macro"
                return "scalp"
        except (TypeError, ValueError):
            pass
    if "MICRO" in origin or "SCALP" in origin or "SNIPER" in origin:
        return "scalp"
    if "SENTINEL" in origin or "MACRO" in origin:
        return "macro"
    if "dynamic_limit" in reason or "open_position" in reason or "broker_attached" in reason:
        return "supervised_exit"
    return "unknown"
_QUEUE_MAX = 2048

_lock = threading.RLock()
_q: queue.SimpleQueue["_JournalEvent | None"] = queue.SimpleQueue()
_stop = threading.Event()
_thread: threading.Thread | None = None
_started = False
_sync_mode = False
_last_flat_ts = 0.0


@dataclass(slots=True)
class _JournalEvent:
    kind: str  # trade_close | flat_session
    ts: float
    deal_id: str
    direction: str
    entry: float | None
    exit: float | None
    pnl_gbp: float | None
    account_id: str = ""
    product_type: str = ""
    engine_origin: str = ""
    exit_reason: str = ""
    hold_sec: float | None = None
    style: str = ""
    epic: str = ""
    ml_score: float | None = None
    regime: str = ""


def reset_performance_journal_for_tests() -> None:
    global _started, _thread, _sync_mode, _last_flat_ts
    stop_performance_journal()
    _sync_mode = False
    _last_flat_ts = 0.0
    _SIMPLIFIED_ACCOUNTING_CACHE["ts"] = 0.0
    _SIMPLIFIED_ACCOUNTING_CACHE["payload"] = None
    _IG_LEDGER_CACHE["ts"] = 0.0
    _IG_LEDGER_CACHE["rows"] = None
    try:
        while True:
            _q.get_nowait()
    except queue.Empty:
        pass


def enable_sync_mode_for_tests(enabled: bool = True) -> None:
    global _sync_mode
    _sync_mode = bool(enabled)


def journal_path() -> Path:
    """Production-only path — ``src/data/v31-production/metrics/`` (no legacy)."""
    from system.paths import v31_production_data_dir

    return v31_production_data_dir() / "metrics" / "daily_journal.csv"


DAILY_MILESTONE_GBP = 1000.0
BENCHMARK_DEAL_ID = "BENCHMARK_OFFSET:£1000_DAILY"


def ensure_benchmark_offset(*, path: Path | None = None) -> Path:
    """Write a clean £1,000 milestone baseline row if the journal is empty/missing."""
    p = path or journal_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_file() and p.stat().st_size > 0:
        text = p.read_text(encoding="utf-8")
        if BENCHMARK_DEAL_ID in text and "Timestamp,DealID,Direction" in text:
            return p
    ts_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        w.writerow(
            [
                ts_iso,
                BENCHMARK_DEAL_ID,
                "",
                "",
                "",
                "0.0",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    return p


def milestone_progress_payload() -> dict[str, Any]:
    """UI status-bar shaped payload — progress vs £1,000 daily milestone."""
    ensure_benchmark_offset()
    realized = daily_realized_pnl_gbp()
    pct = round(min(100.0, max(0.0, 100.0 * realized / DAILY_MILESTONE_GBP)), 2)
    return {
        "ok": True,
        "daily_realized_pnl_gbp": realized,
        "daily_milestone_gbp": DAILY_MILESTONE_GBP,
        "progress_pct": pct,
        "journal": str(journal_path()),
        "benchmark": BENCHMARK_DEAL_ID,
    }


def _fill_telemetry() -> tuple[float | None, float]:
    try:
        from diagnostics.fill_rate_monitor import get_fill_rate_monitor

        mon = get_fill_rate_monitor()
        rate = mon.rolling_fill_rate(20)
        slip = float(mon.current_slip_multiplier())
        return rate, slip
    except Exception:
        return None, 0.5


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _resolve_event_metadata(
    *,
    account_id: str | None = None,
    product_type: str | None = None,
    engine_origin: str | None = None,
    engine_id: str | None = None,
) -> dict[str, str]:
    from system.engine_lane import resolve_journal_metadata

    return resolve_journal_metadata(
        engine_id=engine_id,
        account_id=account_id,
        product_type=product_type,
        engine_origin=engine_origin,
    )


def _ensure_journal_header(path: Path) -> None:
    """Expand legacy CSV headers with v32 accounting columns (non-destructive)."""
    if not path.is_file() or path.stat().st_size == 0:
        return
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        if not rows:
            return
        header = rows[0]
        if header == _HEADER:
            return
        header_set = set(header)
        if "DealID" not in header_set and "Timestamp" not in header_set:
            return
        expanded = list(_HEADER)
        body: list[list[str]] = []
        for r in rows[1:]:
            row_map = {
                header[i]: r[i] if i < len(r) else ""
                for i in range(len(header))
            }
            body.append([row_map.get(col, "") for col in _HEADER])
        with _lock:
            with path.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(expanded)
                w.writerows(body)
    except OSError:
        pass


def _append_row(ev: _JournalEvent) -> None:
    rate, slip = _fill_telemetry()
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.is_file() or path.stat().st_size == 0
    if not write_header:
        _ensure_journal_header(path)
    ts_iso = datetime.fromtimestamp(ev.ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = _resolve_event_metadata(
        account_id=ev.account_id or None,
        product_type=ev.product_type or None,
        engine_origin=ev.engine_origin or None,
    )
    row = [
        ts_iso,
        ev.deal_id,
        ev.direction,
        _fmt(ev.entry),
        _fmt(ev.exit),
        _fmt(None if ev.pnl_gbp is None else round(float(ev.pnl_gbp), 4)),
        _fmt(None if rate is None else round(float(rate), 4)),
        _fmt(round(float(slip), 4)),
        meta["account_id"],
        meta["product_type"],
        meta["engine_origin"],
        str(ev.exit_reason or ""),
        _fmt(None if ev.hold_sec is None else round(float(ev.hold_sec), 1)),
        str(
            ev.style
            or infer_trade_style(
                engine_origin=meta["engine_origin"],
                exit_reason=ev.exit_reason,
                hold_sec=ev.hold_sec,
            )
        ),
    ]
    with _lock:
        with path.open("a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if write_header:
                w.writerow(_HEADER)
            w.writerow(row)


def _worker() -> None:
    while not _stop.is_set():
        try:
            ev = _q.get(timeout=0.5)
        except queue.Empty:
            continue
        if ev is None:
            break
        try:
            _append_row(ev)
        except Exception as exc:
            log_engine(f"PerformanceJournal: write failed {type(exc).__name__}: {exc}")


def start_performance_journal() -> None:
    global _started, _thread
    if _sync_mode or _started:
        return
    _started = True
    _stop.clear()
    _thread = threading.Thread(
        target=_worker,
        name="performance-journal",
        daemon=True,
    )
    _thread.start()


def stop_performance_journal() -> None:
    global _started, _thread
    if _sync_mode:
        return
    _stop.set()
    try:
        _q.put_nowait(None)
    except Exception:
        pass
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=1.0)
    _thread = None
    _started = False


def _enqueue(ev: _JournalEvent) -> None:
    if _sync_mode:
        try:
            _append_row(ev)
        except Exception:
            pass
        return
    if not _started:
        start_performance_journal()
    try:
        # Bound memory: drop oldest on overflow without blocking tick lane.
        if _q.qsize() >= _QUEUE_MAX:
            try:
                _q.get_nowait()
            except queue.Empty:
                pass
        _q.put_nowait(ev)
    except Exception:
        pass


def record_trade_close(
    *,
    deal_id: str,
    direction: str = "",
    entry_price: float | None = None,
    exit_price: float | None = None,
    realized_pnl_gbp: float | None = None,
    closed_at_ts: float | None = None,
    account_id: str | None = None,
    product_type: str | None = None,
    engine_origin: str | None = None,
    engine_id: str | None = None,
    exit_reason: str | None = None,
    hold_sec: float | None = None,
    style: str | None = None,
    epic: str | None = None,
    ml_score: float | None = None,
    regime: str | None = None,
) -> None:
    """Hot-path safe — enqueue one journal line for a closed deal."""
    meta = _resolve_event_metadata(
        account_id=account_id,
        product_type=product_type,
        engine_origin=engine_origin,
        engine_id=engine_id,
    )
    style_tag = infer_trade_style(
        engine_origin=meta["engine_origin"],
        exit_reason=str(exit_reason or ""),
        hold_sec=float(hold_sec) if hold_sec is not None else None,
        style=style,
    )
    _enqueue(
        _JournalEvent(
            kind="trade_close",
            ts=float(closed_at_ts) if closed_at_ts is not None else time.time(),
            deal_id=str(deal_id or "").strip() or "UNKNOWN",
            direction=str(direction or "").upper(),
            entry=float(entry_price) if entry_price is not None else None,
            exit=float(exit_price) if exit_price is not None else None,
            pnl_gbp=float(realized_pnl_gbp) if realized_pnl_gbp is not None else None,
            account_id=meta["account_id"],
            product_type=meta["product_type"],
            engine_origin=meta["engine_origin"],
            exit_reason=str(exit_reason or ""),
            hold_sec=float(hold_sec) if hold_sec is not None else None,
            style=style_tag,
            epic=str(epic or ""),
            ml_score=float(ml_score) if ml_score is not None else None,
            regime=str(regime or ""),
        )
    )
    try:
        from execution.asymmetric_ioc_router import note_closed_trade_outcome

        note_closed_trade_outcome(float(realized_pnl_gbp or 0.0))
    except Exception:
        pass
    # Lightweight ML feedback — overnight monitor scrapes the summary log line.
    try:
        from diagnostics.ml_trade_outcomes import record_ml_trade_outcome

        record_ml_trade_outcome(
            account_id=meta["account_id"],
            epic=str(epic or ""),
            side=str(direction or "").upper(),
            ml_score=float(ml_score) if ml_score is not None else None,
            regime=str(regime or ""),
            style=style_tag,
            pnl=float(realized_pnl_gbp) if realized_pnl_gbp is not None else None,
            deal_id=str(deal_id or "").strip(),
            exit_reason=str(exit_reason or ""),
            hold_sec=float(hold_sec) if hold_sec is not None else None,
            engine_origin=meta["engine_origin"],
        )
    except Exception:
        pass
    # Per-account streak timers (post-win cooldown / post-loss tilt lock).
    # Dedicated state — does NOT arm entry_halt / deploy_hold.
    try:
        from execution.streak_protection import arm_streak_protection_on_close

        arm_streak_protection_on_close(
            account_id=meta.get("account_id") or account_id,
            realized_pnl_gbp=realized_pnl_gbp,
            deal_id=str(deal_id or ""),
        )
    except Exception:
        pass


def journal_has_deal(deal_id: str, *, path: Path | None = None) -> bool:
    """True when *deal_id* already has a row in ``daily_journal.csv``."""
    deal = str(deal_id or "").strip()
    if not deal:
        return False
    p = path or journal_path()
    if not p.is_file():
        return False
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if str(row.get("DealID") or "").strip() == deal:
                    return True
    except OSError:
        return False
    return False


def ensure_broker_attached_exit_journaled(
    *,
    deal_id: str,
    direction: str = "",
    entry_price: float | None = None,
    exit_price: float | None = None,
    realized_pnl_gbp: float | None = None,
    closed_at: str | None = None,
    closed_at_ts: float | None = None,
    account_id: str | None = None,
    product_type: str | None = None,
    engine_origin: str | None = None,
    exit_reason: str | None = None,
    hold_sec: float | None = None,
    style: str | None = None,
    epic: str | None = None,
    path: Path | None = None,
) -> bool:
    """Idempotently journal a broker-attached / ExitGate-bypass close.

    SL/TP and other IG-attached closes skip ``exit_execution_gate`` — without
    this hook those DIAAAA* deals can settle on the broker and never appear in
    ``daily_journal.csv``. Returns True when a new row was written.
    """
    deal = str(deal_id or "").strip().upper()
    if not deal:
        return False
    # Expand short IG close refs so soak milestones see DIAAAA* DealIDs.
    if not deal.startswith("DIAAAA") and len(deal) >= 6 and deal.isalnum():
        deal = f"DIAAAAX{deal}"
    if journal_has_deal(deal, path=path):
        return False
    # Also skip if the short-form row already exists (avoid double-count).
    if deal.startswith("DIAAAAX") and journal_has_deal(deal[8:], path=path):
        return False
    if realized_pnl_gbp is None:
        # Still journal £0 so soak / DealID round-trip is not silent.
        pnl = 0.0
    else:
        try:
            pnl = float(realized_pnl_gbp)
        except (TypeError, ValueError):
            pnl = 0.0

    ts = closed_at_ts
    if ts is None and closed_at:
        raw = str(closed_at).strip().replace("T", " ").replace("Z", "")
        has_clock = ":" in raw and len(raw) >= 16
        for fmt, n in (
            ("%Y-%m-%d %H:%M:%S", 19),
            ("%Y-%m-%d %H:%M", 16),
            ("%Y-%m-%d", 10),
        ):
            try:
                dt = datetime.strptime(raw[:n], fmt)
                if not has_clock and fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=12, minute=0, second=0)
                ts = dt.replace(tzinfo=timezone.utc).timestamp()
                break
            except ValueError:
                continue

    origin = str(engine_origin or "").strip() or "broker_attached"
    reason = str(exit_reason or "").strip() or "broker_attached"
    record_trade_close(
        deal_id=deal,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        realized_pnl_gbp=pnl,
        closed_at_ts=ts,
        account_id=account_id,
        product_type=product_type,
        engine_origin=origin,
        exit_reason=reason,
        hold_sec=hold_sec,
        style=style,
        epic=epic,
    )
    try:
        log_engine(
            f"PerformanceJournal: broker-attached exit journaled deal={deal[:16]} "
            f"pnl={pnl:.2f} origin={origin} reason={reason[:40]}"
        )
    except Exception:
        pass
    return True


def upsert_journal_cash_close(
    *,
    deal_id: str,
    direction: str = "",
    entry_price: float | None = None,
    exit_price: float | None = None,
    realized_pnl_gbp: float | None = None,
    closed_at: str | None = None,
    account_id: str | None = None,
    product_type: str | None = None,
    engine_origin: str | None = None,
    engine_id: str | None = None,
) -> None:
    """Replace £0 stub rows for *deal_id* and append the broker-confirmed cash line.

    Used when IG transaction sync lands after a phantom reconcile wrote entry==exit.
    """
    deal = str(deal_id or "").strip()
    if not deal or realized_pnl_gbp is None:
        return
    try:
        pnl = float(realized_pnl_gbp)
    except (TypeError, ValueError):
        return
    path = journal_path()
    if path.is_file() and path.stat().st_size > 0:
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
            if rows:
                header, body = rows[0], rows[1:]
                kept: list[list[str]] = []
                for r in body:
                    if not r:
                        continue
                    # DealID is column 1 — drop prior stub / zero rows for this deal
                    if len(r) > 1 and str(r[1]).strip() == deal:
                        try:
                            prior_pnl = float(r[5]) if len(r) > 5 and r[5] != "" else 0.0
                        except (TypeError, ValueError):
                            prior_pnl = 0.0
                        try:
                            e = float(r[3]) if len(r) > 3 and r[3] != "" else None
                            x = float(r[4]) if len(r) > 4 and r[4] != "" else None
                        except (TypeError, ValueError):
                            e, x = None, None
                        if abs(prior_pnl) < 1e-9 and (
                            e is None or x is None or abs(e - x) < 1e-9
                        ):
                            continue
                        # Also drop a prior cash row for same deal (idempotent upsert)
                        if abs(prior_pnl) >= 1e-9:
                            continue
                    kept.append(r)
                with _lock:
                    with path.open("w", encoding="utf-8", newline="") as fh:
                        w = csv.writer(fh)
                        w.writerow(header)
                        w.writerows(kept)
        except OSError:
            pass
    closed_ts: float | None = None
    if closed_at:
        raw = str(closed_at).strip().replace("T", " ").replace("Z", "")
        has_clock = ":" in raw and len(raw) >= 16
        # Prefer true close clock time. Date-only IG stamps must NOT become
        # batch-sync wall-clock (cluster) — use local noon for that calendar day.
        formats = (
            ("%Y-%m-%d %H:%M:%S", 19),
            ("%Y-%m-%d %H:%M", 16),
            ("%Y-%m-%d", 10),
        )
        for fmt, n in formats:
            try:
                dt = datetime.strptime(raw[:n], fmt)
                if not has_clock and fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=12, minute=0, second=0)
                closed_ts = dt.replace(tzinfo=timezone.utc).timestamp()
                break
            except ValueError:
                continue
    record_trade_close(
        deal_id=deal,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        realized_pnl_gbp=pnl,
        closed_at_ts=closed_ts,
        account_id=account_id,
        product_type=product_type,
        engine_origin=engine_origin,
        engine_id=engine_id,
    )
    # Bust accounting cache so Terminal sees cash immediately.
    _SIMPLIFIED_ACCOUNTING_CACHE["ts"] = 0.0
    _SIMPLIFIED_ACCOUNTING_CACHE["payload"] = None


def record_flat_session(*, reason: str = "flat") -> None:
    """Journal a flat-book marker (rate-limited to once per 30s)."""
    global _last_flat_ts
    now = time.time()
    if now - _last_flat_ts < 30.0:
        return
    _last_flat_ts = now
    _enqueue(
        _JournalEvent(
            kind="flat_session",
            ts=now,
            deal_id=f"FLAT_SESSION:{reason}",
            direction="",
            entry=None,
            exit=None,
            pnl_gbp=None,
        )
    )


def daily_realized_pnl_gbp(*, path: Path | None = None) -> float:
    """Sum RealizedPnL_GBP for today's UTC journal rows (milestone tracker).

    Ignores benchmark/flat markers and cancelled-style stubs where entry==exit
    with near-zero P&L (and empty direction), so phantom zeros do not dominate.
    """
    p = path or journal_path()
    if not p.is_file():
        return 0.0
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                ts = str(row.get("Timestamp") or "")
                if not ts.startswith(today):
                    continue
                deal = str(row.get("DealID") or "")
                if deal.startswith("FLAT_SESSION") or deal.startswith("BENCHMARK_OFFSET"):
                    continue
                direction = str(row.get("Direction") or "").strip()
                entry_s = str(row.get("EntryPrice") or "").strip()
                exit_s = str(row.get("ExitPrice") or "").strip()
                try:
                    pnl = float(row.get("RealizedPnL_GBP") or 0)
                except (TypeError, ValueError):
                    continue
                # Skip flat cancelled / phantom stubs (entry==exit, ~0 cash)
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
                total += pnl
    except OSError:
        return 0.0
    return round(total, 4)


_EPIC_ASSET_LABELS = {
    "IX.D.DOW.IFM.IP": "DOW",
    "IX.D.NIKKEI.IFM.IP": "NIKKEI",
    "IX.D.FTSE.IFM.IP": "FTSE",
    "IX.D.DAX.IFM.IP": "DAX",
    "CS.D.CFPGOLD.CFP.IP": "GOLD",
    "CS.D.EURUSD.CFD.IP": "EURUSD",
    "CS.D.CRUDE.CFD.IP": "CRUDE",
}


def _resolve_gross_pnl_gbp(row: dict[str, Any]) -> float:
    """True gross from IG ledger semantics — pnl_points × contract_size × point_value."""
    try:
        direct = float(row.get("net_pnl_gbp") or 0)
    except (TypeError, ValueError):
        direct = 0.0
    if abs(direct) > 1e-9:
        return round(direct, 4)
    try:
        from system.pnl_math import settle_gbp_from_ig

        entry = row.get("entry")
        exit_ = row.get("exit")
        size = row.get("size")
        epic = str(row.get("epic") or "")
        if entry is not None and exit_ is not None and size is not None:
            from system.pnl_math import realised_pnl_points

            pts = realised_pnl_points(
                str(row.get("direction") or "BUY"),
                float(entry),
                float(exit_),
            )
            gross = settle_gbp_from_ig(
                pnl_points=pts,
                contract_size=float(size),
                point_value=1.0,
            )
            if gross is not None:
                return round(float(gross), 4)
    except Exception:
        pass
    return round(direct, 4)


def _result_label(pnl_gbp: float) -> str:
    from system.pnl_math import classify_result_gbp

    return classify_result_gbp(float(pnl_gbp))


def _enrich_accounting_row(row: dict[str, Any]) -> dict[str, Any]:
    gross = _resolve_gross_pnl_gbp(row)
    out = dict(row)
    out["net_pnl_gbp"] = gross
    out["result"] = _result_label(gross)
    return out


def _asset_label(epic: str | None, market: str | None = None, deal_id: str = "") -> str:
    epic_s = str(epic or "").strip()
    if epic_s in _EPIC_ASSET_LABELS:
        return _EPIC_ASSET_LABELS[epic_s]
    market_s = str(market or "").strip()
    if market_s:
        # Shorten common IG market names for the desk ledger.
        low = market_s.lower()
        if "wall street" in low or "dow" in low:
            return "DOW"
        if "nikkei" in low or "japan" in low:
            return "NIKKEI"
        if "gold" in low:
            return "GOLD"
        if "eur" in low and "usd" in low:
            return "EURUSD"
        if "ftse" in low or "uk 100" in low:
            return "FTSE"
        return market_s[:18]
    if epic_s:
        parts = epic_s.split(".")
        return parts[2] if len(parts) >= 3 else epic_s[:18]
    deal = str(deal_id or "").strip()
    return deal[:18] if deal else "—"


def _is_zero_stub_row(row: dict[str, Any]) -> bool:
    """True for phantom reconciles / cancelled stubs with no settled cash."""
    try:
        pnl = float(row.get("net_pnl_gbp") or 0)
    except (TypeError, ValueError):
        pnl = 0.0
    if abs(pnl) > 1e-9:
        return False
    entry = row.get("entry")
    exit_ = row.get("exit")
    try:
        if entry is not None and exit_ is not None and abs(float(entry) - float(exit_)) < 1e-9:
            return True
    except (TypeError, ValueError):
        pass
    # Deal-id-as-asset with £0 and no epic is a journal stub.
    asset = str(row.get("asset") or "")
    epic = str(row.get("epic") or "")
    if not epic and asset.startswith("DI"):
        return True
    return False


def _learning_deal_epic_map() -> dict[str, str]:
    """deal_id → epic lookup for enriching journal stubs (local DB, no REST)."""
    try:
        import sqlite3

        from system.paths import data_dir

        db = data_dir() / "learning_db.sqlite3"
        if not db.is_file():
            return {}
        out: dict[str, str] = {}
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
            for deal, epic in con.execute(
                """
                SELECT ig_deal_id, epic FROM trades
                WHERE ig_deal_id IS NOT NULL AND epic IS NOT NULL
                  AND closed_at IS NOT NULL
                ORDER BY id DESC LIMIT 2000
                """
            ):
                d = str(deal or "").strip()
                e = str(epic or "").strip()
                if d and e and d not in out:
                    out[d] = e
        return out
    except Exception:
        return {}


def _learning_row_to_dict(r: Any) -> dict[str, Any] | None:
    result_u = str(r["result"] or "").upper()
    cash = r["ig_pnl_currency"]
    pts = r["pnl_points"]
    size_v = r["size"]
    try:
        if cash is not None:
            pnl = float(cash)
        elif pts is not None and size_v is not None:
            pnl = float(pts) * float(size_v)
        else:
            pnl = 0.0
    except (TypeError, ValueError):
        return None
    if result_u in ("CANCELLED", "REJECTED") and abs(pnl) < 1e-9:
        return None
    ts = str(r["closed_at"] or "").replace(" ", "T")
    if ts and not ts.endswith("Z") and "T" in ts:
        ts = f"{ts}Z" if "+" not in ts else ts
    epic = str(r["epic"] or "")
    deal = str(r["ig_deal_id"] or "")
    return {
        "timestamp": ts,
        "asset": _asset_label(epic, str(r["market"] or ""), deal),
        "direction": str(r["side"] or "—").upper() or "—",
        "net_pnl_gbp": round(pnl, 4),
        "deal_id": deal,
        "epic": epic,
        "entry": r["entry"],
        "exit": r["exit"],
        "size": r["size"],
        "result": _result_label(pnl),
    }


def _learning_db_closed_rows(*, limit: int = 400) -> list[dict[str, Any]]:
    """Closed trades from learning DB — local SoT when journal is £0 stubs.

    Prefers ``ig_pnl_currency``; falls back to ``pnl_points * size`` for
    spreadbet cash. Skips CANCELLED/REJECTED with no cash packet.

    Recent days may be flooded with phantom £0 reconciles — always pull a
    dedicated cash-bearing slice so the desk blotter is not blank.
    """
    try:
        import sqlite3

        from system.paths import data_dir

        db = data_dir() / "learning_db.sqlite3"
        if not db.is_file():
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        lim = int(max(20, limit))
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            # 1) Cash / point-settled closes first (historic blotter)
            cash_rows = con.execute(
                """
                SELECT closed_at, epic, market, side, entry, exit, size,
                       pnl_points, ig_pnl_currency, result, ig_deal_id
                FROM trades
                WHERE closed_at IS NOT NULL
                  AND (
                    (ig_pnl_currency IS NOT NULL AND abs(ig_pnl_currency) > 1e-9)
                    OR (
                      pnl_points IS NOT NULL AND size IS NOT NULL
                      AND abs(pnl_points * size) > 1e-9
                    )
                  )
                ORDER BY closed_at DESC, id DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
            # 2) Fill with newest closes (may include today's stubs)
            recent_rows = con.execute(
                """
                SELECT closed_at, epic, market, side, entry, exit, size,
                       pnl_points, ig_pnl_currency, result, ig_deal_id
                FROM trades
                WHERE closed_at IS NOT NULL
                ORDER BY closed_at DESC, id DESC
                LIMIT ?
                """,
                (min(80, lim),),
            ).fetchall()
        for r in list(cash_rows) + list(recent_rows):
            parsed = _learning_row_to_dict(r)
            if not parsed:
                continue
            key = str(parsed.get("deal_id") or "") or (
                f"{parsed.get('timestamp')}|{parsed.get('asset')}|"
                f"{parsed.get('net_pnl_gbp')}"
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(parsed)
            if len(out) >= lim:
                break
        return out
    except Exception:
        return []


def _journal_closed_rows(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Read settled journal rows (no disk locks — open/read/close)."""
    p = path or journal_path()
    if not p.is_file():
        return []
    deal_map = _learning_deal_epic_map()
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                deal = str(row.get("DealID") or "")
                if deal.startswith("FLAT_SESSION") or deal.startswith("BENCHMARK_OFFSET"):
                    continue
                direction = str(row.get("Direction") or "").strip().upper()
                try:
                    pnl = float(row.get("RealizedPnL_GBP") or 0)
                except (TypeError, ValueError):
                    continue
                ts = str(row.get("Timestamp") or "")
                entry_s = str(row.get("EntryPrice") or "").strip()
                exit_s = str(row.get("ExitPrice") or "").strip()
                try:
                    entry_f = float(entry_s) if entry_s else None
                    exit_f = float(exit_s) if exit_s else None
                except (TypeError, ValueError):
                    entry_f, exit_f = None, None
                epic = deal_map.get(deal, "")
                out.append(
                    _enrich_accounting_row(
                        {
                            "timestamp": ts,
                            "asset": _asset_label(epic, None, deal),
                            "direction": direction or "—",
                            "net_pnl_gbp": round(pnl, 4),
                            "deal_id": deal,
                            "epic": epic,
                            "entry": entry_f,
                            "exit": exit_f,
                            "account_id": str(row.get("AccountID") or ""),
                            "product_type": str(row.get("ProductType") or ""),
                            "engine_origin": str(row.get("EngineOrigin") or ""),
                        }
                    )
                )
    except OSError:
        return []
    return out


def _select_accounting_rows(
    journal_rows: list[dict[str, Any]],
    ig_rows: list[dict[str, Any]] | None,
    learning_rows: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Pick the richest local/remote closed-trade set without hammering IG.

    Priority:
      1) IG ledger when it has any nonzero settled cash
      2) Learning DB when journal is empty or all £0 stubs
      3) Journal CSV (enriched with epic labels)
    """
    if ig_rows:
        if any(abs(float(r.get("net_pnl_gbp") or 0)) > 1e-9 for r in ig_rows):
            return "ig_ledger", ig_rows
        # All-zero IG peek is not useful vs learning history.
    journal_has_cash = any(
        abs(float(r.get("net_pnl_gbp") or 0)) > 1e-9 for r in journal_rows
    )
    learning_has_cash = any(
        abs(float(r.get("net_pnl_gbp") or 0)) > 1e-9 for r in learning_rows
    )
    if learning_has_cash and not journal_has_cash:
        return "learning_db", learning_rows
    if journal_rows:
        return "journal_csv", journal_rows
    if learning_rows:
        return "learning_db", learning_rows
    if ig_rows:
        return "ig_ledger", ig_rows
    return "journal_csv", []


def _last_n_closed_for_display(
    rows: list[dict[str, Any]], *, n: int = 10
) -> list[dict[str, Any]]:
    """Newest-first last N; prefer settled cash so £0 stubs don't dominate.

    Prefer |PnL| ≥ £0.50 when available (skips penny recon noise), else any
    nonzero, else non-stub closes.
    """

    def _sort_key(r: dict[str, Any]) -> str:
        return str(r.get("timestamp") or "")

    ordered = sorted(rows, key=_sort_key, reverse=True)

    def _pnl(r: dict[str, Any]) -> float:
        try:
            return abs(float(r.get("net_pnl_gbp") or 0))
        except (TypeError, ValueError):
            return 0.0

    meaningful = [r for r in ordered if _pnl(r) >= 0.5]
    cash_rows = [r for r in ordered if _pnl(r) > 1e-9]
    pool = (
        meaningful
        if meaningful
        else cash_rows
        if cash_rows
        else [r for r in ordered if not _is_zero_stub_row(r)]
    )
    if not pool:
        pool = ordered
    last: list[dict[str, Any]] = []
    for r in pool[:n]:
        last.append(
            {
                "timestamp": r.get("timestamp") or "—",
                "asset": r.get("asset") or "—",
                "direction": r.get("direction") or "—",
                "net_pnl_gbp": float(r.get("net_pnl_gbp") or 0),
            }
        )
    return last


def _ig_ledger_closed_rows(*, days_back: int = 30) -> list[dict[str, Any]] | None:
    """Official IG transaction history — SoT for settled cash when REST is free."""
    try:
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import get_shared_rest_client
        from system.ig_transactions import parse_ig_transaction_row

        status = try_load_credentials()
        if not status.ok or status.credentials is None:
            return None
        rest = get_shared_rest_client(status.credentials)
        hours = max(24.0, float(days_back) * 24.0)
        txns = list(rest.fetch_transaction_history(hours=hours) or [])
        rows: list[dict[str, Any]] = []
        for txn in txns:
            if not isinstance(txn, dict):
                continue
            parsed = parse_ig_transaction_row(txn)
            if not parsed:
                continue
            pnl = parsed.get("ig_pnl_currency")
            if pnl is None:
                pnl = parsed.get("pnl_points")
            if pnl is None:
                continue
            rows.append(
                {
                    "timestamp": str(parsed.get("closed_at") or ""),
                    "asset": str(parsed.get("market") or parsed.get("epic") or "—"),
                    "direction": str(parsed.get("side") or "—").upper(),
                    "net_pnl_gbp": round(float(pnl), 4),
                    "deal_id": str(
                        parsed.get("ig_deal_id") or parsed.get("deal_reference") or ""
                    ),
                }
            )
        return rows
    except Exception:
        return None


def _ig_ledger_closed_rows_bounded(*, days_back: int = 14, timeout_sec: float = 2.5):
    """Non-blocking IG ledger peek — never stall the API on REST budget waits."""
    box: dict[str, Any] = {"rows": None}

    def _worker() -> None:
        box["rows"] = _ig_ledger_closed_rows(days_back=days_back)

    t = threading.Thread(target=_worker, name="ig-ledger-peek", daemon=True)
    t.start()
    t.join(timeout=max(0.5, float(timeout_sec)))
    if t.is_alive():
        return None
    return box.get("rows")


_IG_LEDGER_CACHE: dict[str, Any] = {"ts": 0.0, "rows": None}
_IG_LEDGER_CACHE_TTL_SEC = 120.0
_SIMPLIFIED_ACCOUNTING_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
# 20s payload cache — Terminal polls must not rebuild ledger every few seconds
# (empty flash / journal stub races). Learning-db fallback stays sticky in cache.
_SIMPLIFIED_ACCOUNTING_TTL_SEC = 20.0


def _ig_ledger_rows_cached(*, days_back: int = 14) -> list[dict[str, Any]] | None:
    """At most one IG history peek per TTL — journal remains the hot path."""
    now = time.time()
    cached = _IG_LEDGER_CACHE.get("rows")
    age = now - float(_IG_LEDGER_CACHE.get("ts") or 0.0)
    if cached is not None and age < _IG_LEDGER_CACHE_TTL_SEC:
        return cached  # type: ignore[return-value]
    # Skip IG history when global ledger / positions budget is already hot.
    try:
        from system import shared_rest_budget

        if shared_rest_budget.over_global_limit("ig_ledger", 1.0) or (
            shared_rest_budget.recent_count("ig_positions") >= 8
        ):
            return cached  # type: ignore[return-value]
    except Exception:
        pass
    rows = _ig_ledger_closed_rows_bounded(days_back=days_back, timeout_sec=1.5)
    if rows is not None:
        _IG_LEDGER_CACHE["ts"] = now
        _IG_LEDGER_CACHE["rows"] = rows
        return rows
    return cached  # type: ignore[return-value]



def _compute_intraday_performance_metrics(
    rows: list[dict[str, Any]],
    *,
    today: str,
) -> dict[str, Any]:
    """Intra-day Sharpe, profit factor, and true W/L from closed PnL rows.

    Breakeven stubs (|GBP| < epsilon) are excluded from all W/L, PF, and Sharpe
    math — only true WIN/LOSS cash outcomes count.
    """
    import math

    from system.pnl_math import classify_result_gbp

    raw_pnls: list[float] = []
    for r in rows:
        ts = str(r.get("timestamp") or "")
        day = ts[:10] if len(ts) >= 10 else ""
        if day != today and not ts.startswith(today):
            continue
        try:
            raw_pnls.append(float(r.get("net_pnl_gbp") or 0))
        except (TypeError, ValueError):
            continue
    sample_scope = "today"
    if not raw_pnls:
        for r in rows[-20:]:
            try:
                raw_pnls.append(float(r.get("net_pnl_gbp") or 0))
            except (TypeError, ValueError):
                continue
        sample_scope = "last_20"

    breakeven_n = sum(
        1 for p in raw_pnls if classify_result_gbp(p) == "BREAKEVEN"
    )
    pnls = [p for p in raw_pnls if classify_result_gbp(p) != "BREAKEVEN"]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    win_n = len(wins)
    loss_n = len(losses)
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    if gross_losses > 1e-12:
        profit_factor = gross_wins / gross_losses
    elif gross_wins > 0:
        profit_factor = 99.0
    else:
        profit_factor = 0.0
    win_loss_ratio = (win_n / loss_n) if loss_n > 0 else (float(win_n) if win_n else 0.0)
    win_rate = (win_n / n) if n else 0.0
    sharpe = 0.0
    if n >= 2:
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / max(1, n - 1)
        std = math.sqrt(max(0.0, var))
        if std > 1e-12:
            sharpe = (mean / std) * math.sqrt(min(n, 252))
    net_true = gross_wins - gross_losses
    return {
        "intraday_sharpe": round(float(sharpe), 4),
        "profit_factor": round(float(profit_factor), 4),
        "win_loss_ratio": round(float(win_loss_ratio), 4),
        "win_rate": round(float(win_rate), 4),
        "wins": int(win_n),
        "losses": int(loss_n),
        "true_wins": int(win_n),
        "true_losses": int(loss_n),
        "true_win_rate": round(float(win_rate), 4),
        "true_win_loss_ratio": round(float(win_loss_ratio), 4),
        "gross_wins_gbp": round(float(gross_wins), 4),
        "gross_losses_gbp": round(float(gross_losses), 4),
        "net_true_outcome_gbp": round(float(net_true), 4),
        "breakeven_excluded": int(breakeven_n),
        "sample_n": int(n),
        "raw_sample_n": int(len(raw_pnls)),
        "sample_scope": sample_scope,
    }


def simplified_accounting_payload() -> dict[str, Any]:
    """Sovereign desk accounting — three verified datasets only.

    a) Today's net realized P&L (settled cash)
    b) Last 10 closed trades (Timestamp | Asset | Direction | Net P&L)
    c) Day-by-day historical settled profits (Date | P&L)

    Journal is always available instantly. IG history is attempted sparsely
    (cached ≥120s) so Terminal polls never storm the 3/min REST budget.
    """
    now = time.time()
    cached_payload = _SIMPLIFIED_ACCOUNTING_CACHE.get("payload")
    if (
        isinstance(cached_payload, dict)
        and now - float(_SIMPLIFIED_ACCOUNTING_CACHE.get("ts") or 0.0)
        < _SIMPLIFIED_ACCOUNTING_TTL_SEC
    ):
        return dict(cached_payload)

    journal_rows = _journal_closed_rows()
    # Prefer journal for the desk hot path. IG ledger is enrichment only when
    # the budget allows — never block or overwrite a healthy journal day with
    # a failed/partial REST peek. Learning DB fills gaps when journal is £0 stubs.
    ig_rows = _ig_ledger_rows_cached(days_back=14)
    learning_rows = _learning_db_closed_rows(limit=500)
    source, rows = _select_accounting_rows(journal_rows, ig_rows, learning_rows)

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    today_net = 0.0
    by_day: dict[str, float] = {}
    for r in rows:
        ts = str(r.get("timestamp") or "")
        day = ts[:10] if len(ts) >= 10 else ""
        try:
            pnl = float(r.get("net_pnl_gbp") or 0)
        except (TypeError, ValueError):
            continue
        if day:
            by_day[day] = round(by_day.get(day, 0.0) + pnl, 4)
        if day == today or ts.startswith(today):
            today_net += pnl

    # Journal milestone sum is authoritative for "today" when using local sources
    if source in ("journal_csv", "learning_db") or abs(today_net) < 1e-12:
        journal_today = daily_realized_pnl_gbp()
        if source == "journal_csv" or abs(journal_today) > 1e-12:
            today_net = journal_today
        elif source == "learning_db":
            # Keep learning-derived today_net (already summed above)
            pass

    # Last 10: prefer rows with real GBP so phantom £0 stubs don't blank the ledger
    last_10 = [_enrich_accounting_row(r) for r in _last_n_closed_for_display(rows, n=10)]
    # If selected source still yields empty last_10 but learning has cash history,
    # surface those older settles anyway (empty_day today ≠ no historic blotter).
    if (
        not last_10
        or (
            all(abs(float(r.get("net_pnl_gbp") or 0)) < 1e-9 for r in last_10)
            and any(abs(float(r.get("net_pnl_gbp") or 0)) > 1e-9 for r in learning_rows)
        )
    ):
        last_10 = [_enrich_accounting_row(r) for r in _last_n_closed_for_display(learning_rows, n=10)]
        if last_10 and source == "journal_csv":
            source = "learning_db"
        # Merge day history from learning when journal days are all zero
        if learning_rows and (
            not by_day or all(abs(v) < 1e-9 for v in by_day.values())
        ):
            by_day = {}
            for r in learning_rows:
                ts = str(r.get("timestamp") or "")
                day = ts[:10] if len(ts) >= 10 else ""
                if not day:
                    continue
                try:
                    pnl = float(r.get("net_pnl_gbp") or 0)
                except (TypeError, ValueError):
                    continue
                by_day[day] = round(by_day.get(day, 0.0) + pnl, 4)

    # Newest calendar day first — Settled Cash Ledger day-by-day blotter
    daily_history = [
        {"date": d, "pnl_gbp": by_day[d]}
        for d in sorted(by_day.keys(), reverse=True)
    ]

    health: dict[str, Any] = {}
    try:
        from runtime.feed_health_watchdog import system_health_snapshot

        health = system_health_snapshot()
    except Exception:
        health = {"is_healthy": True, "quote_age_sec": None}

    trading_path: dict[str, Any] = {}
    try:
        from runtime.trading_path_readiness import compute_trading_path_readiness

        trading_path = compute_trading_path_readiness()
    except Exception:
        trading_path = {"trading_path_live": False, "badge": "DESK TRADING DOWN"}

    # empty_day = no settled cash *today* — last_10 may still show older PnL rows
    empty_day = abs(float(today_net)) < 1e-9
    perf = _compute_intraday_performance_metrics(rows, today=today)
    path_live = bool(trading_path.get("trading_path_live"))
    # Emerald badge requires trading path live — quote age alone is a feed lie.
    operational = path_live and bool(health.get("operational_badge") or health.get("is_healthy"))
    desk_rag = "G" if path_live and operational else "A"
    desk_rag_label = "GREEN — path live" if desk_rag == "G" else "AMBER — path not live"
    try:
        from runtime.desk_dev_controls import entries_paused
        from system.rest_api_budget import get_rest_api_budget

        level = str(get_rest_api_budget().metrics().get("pressure_level") or "IDLE").upper()
        if level == "CRITICAL":
            desk_rag, desk_rag_label = "R", "RED — REST critical"
        elif entries_paused() or level in ("HIGH", "ELEVATED"):
            desk_rag, desk_rag_label = "A", "AMBER — entries paused or REST pressure"
    except Exception:
        pass
    payload = {
        "ok": True,
        "source": source,
        "empty_day": empty_day,
        "today_net_realized_pnl_gbp": round(float(today_net), 4),
        "last_10_closed_trades": last_10,
        "daily_history": daily_history,
        "performance_metrics": perf,
        "system_state": {
            "is_healthy": bool(health.get("is_healthy", True)),
            "quote_age_sec": health.get("quote_age_sec"),
            "quote_age_ms": health.get("quote_age_ms"),
            "operational_badge": operational,
            "trading_path_live": path_live,
            "trading_path_badge": str(trading_path.get("badge") or ""),
            "trading_path_blockers": list(trading_path.get("blockers") or []),
            "trading_path_primary": trading_path.get("primary_blocker"),
            "entries_blocked": bool(health.get("entries_blocked")) or (not path_live),
            "desk_rag": desk_rag,
            "desk_rag_label": desk_rag_label,
            "last_reason": (
                (trading_path.get("primary_blocker") or {}).get("label")
                or health.get("last_reason")
                or ""
            ),
        },
    }
    _SIMPLIFIED_ACCOUNTING_CACHE["ts"] = now
    _SIMPLIFIED_ACCOUNTING_CACHE["payload"] = payload
    return dict(payload)
