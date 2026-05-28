#!/usr/bin/env python3
"""
Fetch Japan 225 MINUTE_5 OHLC from IG REST into append-only JSONL cache.

  PYTHONPATH=src python3 scripts/fetch_historical_ohlc.py

Allowed only 07:00–22:30 Europe/London (quiet 22:30–07:00 for live REST budget).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.credentials_loader import try_load_credentials
from system.engine_log import log_engine
from system.ig_rest_session import ensure_shared_authenticated
from system.paths import data_dir
from system.ohlc_fetch_window import in_ohlc_fetch_quiet_window
from system.rest_api_budget import RestBudgetPausedError, ohlc_bootstrap_rest_window
from trading.ohlc_bootstrap import _parse_bar_time

EPIC = "IX.D.NIKKEI.IFM.IP"
RESOLUTION = "MINUTE_5"
DATE_FROM_DEFAULT = "2024-01-01T00:00:00"
PAGE_SIZE = 1000
REST_SLEEP_SEC = 12.0
PROGRESS_EVERY = 500
CACHE_PATH = data_dir() / "ohlc_cache" / "nikkei_5m.jsonl"
STATUS_PATH = data_dir() / "state" / "ohlc_pull_status.json"
LONDON = ZoneInfo("Europe/London")
ALLOWANCE_ERR = "error.public-api.exceeded-account-historical-data-allowance"
RATE_LIMIT_HINTS = (
    "error.public-api.exceeded-api-key-allowance",
    "error.public-api.exceeded-account-allowance",
    "error.public-api.exceeded-account-trading-allowance",
    "too_many_requests",
)
MAX_FETCH_RETRIES = 4
BASE_BACKOFF_SEC = 12.0
MAX_BACKOFF_SEC = 240.0
ALLOWANCE_RETRY_MINUTES = 45
RATE_LIMIT_RETRY_MINUTES = 12
THROTTLE_RETRY_MINUTES = 5


def _iso_bar_time(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(LONDON).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _mid_from_price_obj(obj: Any) -> float:
    if isinstance(obj, dict):
        for key in ("mid", "lastTraded", "bid"):
            v = obj.get(key)
            if v is not None and float(v) > 0:
                return float(v)
        ask = obj.get("ask") or obj.get("offer")
        bid = obj.get("bid")
        if bid is not None and ask is not None and float(ask) > float(bid):
            return (float(bid) + float(ask)) / 2.0
    try:
        return float(obj or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_ig_candle(row: dict) -> dict | None:
    snap = row.get("snapshotTime") or row.get("snapshotTimeUTC") or ""
    op = row.get("openPrice") or {}
    hi = row.get("highPrice") or {}
    lo = row.get("lowPrice") or {}
    cl = row.get("closePrice") or {}
    o = _mid_from_price_obj(op)
    h = _mid_from_price_obj(hi)
    low = _mid_from_price_obj(lo)
    c = _mid_from_price_obj(cl)
    if c <= 0 and low > 0:
        c = low
    if h <= 0 or low <= 0 or c <= 0:
        return None
    bid_c = float((cl.get("bid") if isinstance(cl, dict) else 0) or 0)
    ask_c = float((cl.get("ask") if isinstance(cl, dict) else 0) or cl.get("offer") or 0)
    spread = max(0.0, ask_c - bid_c) if ask_c > bid_c else max(1.0, h - low)
    vol = float(row.get("lastTradedVolume") or row.get("volume") or 0)
    t = _iso_bar_time(_parse_bar_time(str(snap)))
    return {
        "t": t,
        "o": round(o, 1),
        "h": round(h, 1),
        "l": round(low, 1),
        "c": round(c, 1),
        "v": round(vol, 1),
        "spread": round(spread, 1),
    }


def _read_last_timestamp(path: Path) -> str | None:
    if not path.is_file():
        return None
    last_line = ""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last_line = line.strip()
    if not last_line:
        return None
    try:
        return str(json.loads(last_line).get("t") or "")
    except json.JSONDecodeError:
        return None


def _load_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _resume_from(last_ts: str | None, default_from: str) -> str:
    if not last_ts:
        return default_from
    try:
        dt = datetime.fromisoformat(last_ts)
    except ValueError:
        return default_from
    return _iso_bar_time(dt + timedelta(minutes=5))


def _backoff_seconds(attempt: int) -> float:
    return min(MAX_BACKOFF_SEC, BASE_BACKOFF_SEC * (2 ** max(0, attempt - 1)))


def _extract_error_code(response: Any) -> str:
    body = ""
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = str(parsed.get("errorCode") or parsed.get("error") or "")
    except Exception:
        body = ""
    raw = (body or response.text or "").strip().lower()
    return raw


def _is_rate_limit_error(status_code: int, err: str) -> bool:
    if status_code == 429:
        return True
    if status_code != 403:
        return False
    return any(hint in err for hint in RATE_LIMIT_HINTS)


def _next_retry_iso(minutes: int) -> str:
    return _iso_bar_time(datetime.now(LONDON) + timedelta(minutes=minutes))


def _blocked_status(
    *,
    block_reason: str,
    next_retry_time: str,
    detail: str,
) -> dict[str, str]:
    return {
        "block_reason": block_reason,
        "next_retry_time": next_retry_time,
        "detail": detail,
    }


def _fetch_page(
    rest: Any,
    *,
    epic: str,
    page_number: int,
    date_from: str,
    date_to: str,
) -> tuple[list[dict], dict, dict[str, str] | None]:
    params = {
        "resolution": RESOLUTION,
        "from": date_from,
        "to": date_to,
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
    }
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            with ohlc_bootstrap_rest_window():
                r = rest.request(
                    "GET",
                    f"/prices/{epic}",
                    params=params,
                    headers=rest._auth_headers("3"),
                )
        except RestBudgetPausedError as e:
            reason = str(e).strip().lower()
            if reason == "preemptive_throttle":
                return [], {}, _blocked_status(
                    block_reason="throttle",
                    next_retry_time=_next_retry_iso(THROTTLE_RETRY_MINUTES),
                    detail="rest_budget_preemptive_throttle",
                )
            sleep_s = _backoff_seconds(attempt)
            log_engine(
                f"OHLC fetch backoff: rest budget paused ({reason}) attempt={attempt}/{MAX_FETCH_RETRIES} sleep={sleep_s:.1f}s"
            )
            if attempt >= MAX_FETCH_RETRIES:
                return [], {}, _blocked_status(
                    block_reason="rate_limit",
                    next_retry_time=_next_retry_iso(RATE_LIMIT_RETRY_MINUTES),
                    detail=f"rest_budget_{reason or 'paused'}",
                )
            time.sleep(sleep_s)
            continue

        err = _extract_error_code(r)
        if r.status_code == 200:
            body = r.json()
            prices = body.get("prices") or []
            meta = body.get("metadata") or {}
            page_data = meta.get("pageData") or {}
            return prices, page_data, None
        if ALLOWANCE_ERR in err:
            return [], {}, _blocked_status(
                block_reason="allowance",
                next_retry_time=_next_retry_iso(ALLOWANCE_RETRY_MINUTES),
                detail=ALLOWANCE_ERR,
            )
        if _is_rate_limit_error(r.status_code, err):
            sleep_s = _backoff_seconds(attempt)
            log_engine(
                f"OHLC fetch backoff: IG rate-limit signal status={r.status_code} attempt={attempt}/{MAX_FETCH_RETRIES} sleep={sleep_s:.1f}s"
            )
            if attempt >= MAX_FETCH_RETRIES:
                return [], {}, _blocked_status(
                    block_reason="rate_limit",
                    next_retry_time=_next_retry_iso(RATE_LIMIT_RETRY_MINUTES),
                    detail=err or f"http_{r.status_code}",
                )
            time.sleep(sleep_s)
            continue
        raise RuntimeError(f"IG prices HTTP {r.status_code}: {(r.text or '')[:200]}")
    return [], {}, _blocked_status(
        block_reason="other",
        next_retry_time=_next_retry_iso(RATE_LIMIT_RETRY_MINUTES),
        detail="retries_exhausted",
    )


def _append_bars(path: Path, bars: list[dict], seen: set[str], last_written: str) -> tuple[int, str]:
    if not bars:
        return 0, last_written
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted((b for b in bars if isinstance(b, dict)), key=lambda x: str(x.get("t") or ""))
    added = 0
    pending: list[str] = []
    max_written = last_written
    batch_seen: set[str] = set()
    with path.open("a", encoding="utf-8") as handle:
        for bar in ordered:
            key = str(bar.get("t") or "")
            if not key or key in seen or key in batch_seen:
                continue
            if max_written and key <= max_written:
                continue
            seen.add(key)
            batch_seen.add(key)
            pending.append(json.dumps(bar, separators=(",", ":")) + "\n")
            if not max_written or key > max_written:
                max_written = key
            added += 1
        if pending:
            handle.write("".join(pending))
            handle.flush()
            os.fsync(handle.fileno())
    return added, max_written


def main() -> int:
    if in_ohlc_fetch_quiet_window():
        msg = (
            "OHLC fetch skipped: quiet window 22:30–07:00 Europe/London "
            "(allowed 07:00–22:30). Re-run during the allowed window."
        )
        print(msg)
        log_engine(msg)
        return 0

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        print(f"FAIL: credentials — {status.error}", file=sys.stderr)
        return 1

    rest = ensure_shared_authenticated(status.credentials)
    cache_path = CACHE_PATH
    status_path = STATUS_PATH
    pull_status = _load_status(status_path)
    now_iso = _iso_bar_time(datetime.now(LONDON))
    blocked_until = str(pull_status.get("next_retry_time") or "")
    block_reason_prev = str(pull_status.get("block_reason") or "")
    if blocked_until and block_reason_prev:
        try:
            if datetime.fromisoformat(now_iso) < datetime.fromisoformat(blocked_until):
                msg = (
                    "OHLC pull paused: "
                    f"block_reason={block_reason_prev} next_retry={blocked_until}"
                )
                print(msg)
                log_engine(msg)
                pull_status.update(
                    {
                        "status": "blocked_waiting_retry",
                        "last_run_time": now_iso,
                    }
                )
                _save_status(status_path, pull_status)
                return 0
        except ValueError:
            pass
    last_ts = _read_last_timestamp(cache_path)
    date_from = _resume_from(last_ts, DATE_FROM_DEFAULT)
    date_to = _iso_bar_time(datetime.now(LONDON))

    if last_ts:
        print(f"Resume from {date_from} (last cached bar {last_ts})")
    else:
        print(f"Fresh fetch from {date_from} to {date_to}")

    seen: set[str] = set()
    if cache_path.is_file():
        with cache_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line).get("t")
                    if t:
                        seen.add(str(t))
                except json.JSONDecodeError:
                    continue

    total_added = 0
    allowance_blocked = False
    blocked: dict[str, str] | None = None
    next_progress = PROGRESS_EVERY if len(seen) < PROGRESS_EVERY else (
        (len(seen) // PROGRESS_EVERY) + 1
    ) * PROGRESS_EVERY
    chunk_start = datetime.fromisoformat(date_from)
    end_dt = datetime.fromisoformat(date_to)
    chunk_days = 28
    last_written = last_ts or ""

    pull_status.update(
        {
            "status": "running",
            "last_run_time": now_iso,
            "last_attempted_range": {"from": date_from, "to": date_to},
            "block_reason": None,
            "next_retry_time": None,
        }
    )
    _save_status(status_path, pull_status)

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end_dt)
        chunk_from = _iso_bar_time(chunk_start)
        chunk_to = _iso_bar_time(chunk_end)
        page = 1
        total_pages = 1

        while page <= total_pages:
            try:
                raw_rows, page_data, blocked = _fetch_page(
                    rest,
                    epic=EPIC,
                    page_number=page,
                    date_from=chunk_from,
                    date_to=chunk_to,
                )
            except RuntimeError:
                raise
            pull_status["last_attempted_range"] = {"from": chunk_from, "to": chunk_to}
            _save_status(status_path, pull_status)
            if blocked:
                reason = blocked.get("block_reason", "other")
                if reason == "allowance" and chunk_days > 1:
                    chunk_days = max(1, chunk_days // 2)
                    note = (
                        f"Historical allowance gate for {chunk_from}..{chunk_to}; "
                        f"retrying with {chunk_days}-day chunks"
                    )
                    print(note)
                    log_engine(note)
                    total_pages = 0
                    page = 1
                    blocked = None
                    break
                allowance_blocked = reason == "allowance"
                note = (
                    f"OHLC fetch blocked: reason={reason} "
                    f"next_retry={blocked.get('next_retry_time', 'n/a')} "
                    f"detail={blocked.get('detail', '')}"
                )
                print(note)
                log_engine(note)
                total_pages = 0
                page = 1
                break
            parsed: list[dict] = []
            for row in raw_rows:
                bar = _parse_ig_candle(row)
                if bar:
                    parsed.append(bar)
            added, last_written = _append_bars(cache_path, parsed, seen, last_written)
            total_added += added
            if added > 0:
                pull_status["last_success_timestamp"] = last_written
                _save_status(status_path, pull_status)

            while len(seen) >= next_progress:
                sample_t = parsed[-1]["t"][:10] if parsed else chunk_to[:10]
                msg = f"Fetched {len(seen)} bars ({sample_t})..."
                print(msg)
                log_engine(msg)
                next_progress += PROGRESS_EVERY

            total_pages = int(page_data.get("totalPages") or 1) or 1
            if total_pages <= 0:
                break
            if page >= total_pages:
                break
            page += 1
            time.sleep(REST_SLEEP_SEC)

        if allowance_blocked:
            break
        if blocked:
            break
        if page == 1 and total_pages == 0:
            # chunk size was reduced; retry this same starting point.
            continue
        chunk_start = chunk_end + timedelta(minutes=5)
        if chunk_start < end_dt:
            time.sleep(REST_SLEEP_SEC)

    file_size = cache_path.stat().st_size if cache_path.is_file() else 0
    first_ts = ""
    last_out = _read_last_timestamp(cache_path) or ""
    if cache_path.is_file():
        with cache_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    try:
                        ts = str(json.loads(line).get("t") or "")
                        if ts and not first_ts:
                            first_ts = ts
                        if ts:
                            last_out = ts
                    except json.JSONDecodeError:
                        pass

    print("=== FETCH SUMMARY ===")
    print(f"Epic: {EPIC} resolution: {RESOLUTION}")
    print(f"Bars added this run: {total_added}")
    print(f"Total bars in cache: {len(seen)}")
    print(f"Date range: {first_ts or 'n/a'} to {last_out or 'n/a'}")
    print(f"File: {cache_path}")
    print(f"File size: {file_size:,} bytes")
    if blocked:
        pull_status.update(
            {
                "status": "blocked",
                "block_reason": blocked.get("block_reason"),
                "next_retry_time": blocked.get("next_retry_time"),
                "last_run_time": _iso_bar_time(datetime.now(LONDON)),
                "last_success_timestamp": last_out or pull_status.get("last_success_timestamp"),
            }
        )
        _save_status(status_path, pull_status)
    else:
        pull_status.update(
            {
                "status": "ok",
                "block_reason": None,
                "next_retry_time": None,
                "last_run_time": _iso_bar_time(datetime.now(LONDON)),
                "last_success_timestamp": last_out or pull_status.get("last_success_timestamp"),
            }
        )
        _save_status(status_path, pull_status)
    if allowance_blocked:
        print("Status: partial (historical allowance gate hit)")
    elif blocked:
        print(f"Status: partial ({blocked.get('block_reason')})")
    print(f"Pull status: {status_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
