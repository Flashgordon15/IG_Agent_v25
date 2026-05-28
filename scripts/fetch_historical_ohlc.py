#!/usr/bin/env python3
"""
Fetch Japan 225 MINUTE_5 OHLC from IG REST into append-only JSONL cache.

  PYTHONPATH=src python3 scripts/fetch_historical_ohlc.py

Refuses to run 22:30–07:00 Europe/London (live session REST budget protection).
"""

from __future__ import annotations

import json
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
from trading.ohlc_bootstrap import _parse_bar_time

EPIC = "IX.D.NIKKEI.IFM.IP"
RESOLUTION = "MINUTE_5"
DATE_FROM_DEFAULT = "2024-01-01T00:00:00"
PAGE_SIZE = 1000
REST_SLEEP_SEC = 12.0
PROGRESS_EVERY = 500
CACHE_PATH = data_dir() / "ohlc_cache" / "nikkei_5m.jsonl"
LONDON = ZoneInfo("Europe/London")


def _in_live_trading_window(now: datetime | None = None) -> bool:
    """True during 22:30–07:00 BST — do not consume REST budget for backfill."""
    now = now or datetime.now(LONDON)
    minutes = now.hour * 60 + now.minute
    return minutes >= 22 * 60 + 30 or minutes < 7 * 60


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


def _resume_from(last_ts: str | None, default_from: str) -> str:
    if not last_ts:
        return default_from
    try:
        dt = datetime.fromisoformat(last_ts)
    except ValueError:
        return default_from
    return _iso_bar_time(dt + timedelta(minutes=5))


def _fetch_page(
    rest: Any,
    *,
    epic: str,
    page_number: int,
    date_from: str,
    date_to: str,
) -> tuple[list[dict], dict]:
    params = {
        "resolution": RESOLUTION,
        "from": date_from,
        "to": date_to,
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
    }
    r = rest.request(
        "GET",
        f"/prices/{epic}",
        params=params,
        headers=rest._auth_headers("3"),
    )
    if r.status_code != 200:
        raise RuntimeError(f"IG prices HTTP {r.status_code}: {(r.text or '')[:200]}")
    body = r.json()
    prices = body.get("prices") or []
    meta = body.get("metadata") or {}
    page_data = meta.get("pageData") or {}
    return prices, page_data


def _append_bars(path: Path, bars: list[dict], seen: set[str]) -> int:
    if not bars:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with path.open("a", encoding="utf-8") as handle:
        for bar in bars:
            key = str(bar.get("t") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            handle.write(json.dumps(bar, separators=(",", ":")) + "\n")
            added += 1
    return added


def main() -> int:
    if _in_live_trading_window():
        msg = (
            "SKIP: fetch_historical_ohlc blocked during live trading window "
            "(22:30–07:00 Europe/London). Re-run outside this window."
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
    next_progress = PROGRESS_EVERY if len(seen) < PROGRESS_EVERY else (
        (len(seen) // PROGRESS_EVERY) + 1
    ) * PROGRESS_EVERY
    chunk_start = datetime.fromisoformat(date_from)
    end_dt = datetime.fromisoformat(date_to)

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(days=28), end_dt)
        chunk_from = _iso_bar_time(chunk_start)
        chunk_to = _iso_bar_time(chunk_end)
        page = 1
        total_pages = 1

        while page <= total_pages:
            raw_rows, page_data = _fetch_page(
                rest,
                epic=EPIC,
                page_number=page,
                date_from=chunk_from,
                date_to=chunk_to,
            )
            parsed: list[dict] = []
            for row in raw_rows:
                bar = _parse_ig_candle(row)
                if bar:
                    parsed.append(bar)
            parsed.sort(key=lambda b: b["t"])
            added = _append_bars(cache_path, parsed, seen)
            total_added += added

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
