#!/usr/bin/env python3
"""
Fetch Finnhub economic calendar → data_lake/external/ for v26 news guard.

  export FINNHUB_API_KEY=your_key
  PYTHONPATH=src python3 scripts/ingest_finnhub_calendar.py
  PYTHONPATH=src python3 scripts/ingest_finnhub_calendar.py --days 14
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]

_PLACEHOLDER_KEYS = frozenset(
    {
        "",
        "your_key",
        "your_finnhub_key",
        "your_finnhub_key_here",
        "paste_your_finnhub_key_here",
        "YOUR_FINNHUB_KEY",
    }
)
sys.path.insert(0, str(ROOT / "src"))

from system.external_keys import finnhub_api_key

# IG epics affected by macro prints (expand as universe grows)
DEFAULT_MARKETS = [
    "IX.D.DOW.IFM.IP",
    "IX.D.NASDAQ.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.GBPUSD.CFD.IP",
]

COUNTRY_MARKETS: dict[str, list[str]] = {
    "US": [
        "IX.D.DOW.IFM.IP",
        "IX.D.NASDAQ.IFM.IP",
        "CS.D.CFPGOLD.CFP.IP",
        "CS.D.EURUSD.CFD.IP",
        "CS.D.GBPUSD.CFD.IP",
    ],
    "EU": ["CS.D.EURUSD.CFD.IP", "CS.D.CFPGOLD.CFP.IP"],
    "GB": ["CS.D.GBPUSD.CFD.IP", "CS.D.CFPGOLD.CFP.IP"],
    "JP": ["IX.D.NIKKEI.IFM.IP"],
}


def _markets_for_country(country: str) -> list[str]:
    c = (country or "").upper()
    return list(COUNTRY_MARKETS.get(c, DEFAULT_MARKETS))


def _parse_event_time(row: dict) -> datetime | None:
    """Finnhub returns either ``time`` as full datetime or separate ``date`` + ``time``."""
    raw_time = str(row.get("time") or "").strip()
    date_s = str(row.get("date") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        if raw_time:
            try:
                return datetime.strptime(raw_time, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    if date_s:
        time_s = raw_time or "00:00"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                if fmt == "%Y-%m-%d":
                    return datetime.strptime(date_s, fmt).replace(tzinfo=timezone.utc)
                return datetime.strptime(f"{date_s} {time_s}", fmt).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
    return None


def _key_source_hint() -> str:
    import os

    if os.environ.get("FINNHUB_API_KEY", "").strip():
        return "FINNHUB_API_KEY env var (overrides config/external_keys.json)"
    return "config/external_keys.json"


def fetch_economic_calendar(*, api_key: str, days: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=max(days, 1))
    url = "https://finnhub.io/api/v1/calendar/economic"
    params = {
        "from": today.isoformat(),
        "to": end.isoformat(),
        "token": api_key,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("economicCalendar") if isinstance(data, dict) else []
    if not isinstance(raw, list):
        return []

    events: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        impact = str(row.get("impact") or "").lower()
        if impact not in ("high", "3"):
            continue
        dt = _parse_event_time(row)
        if dt is None:
            continue
        country = str(row.get("country") or "")
        events.append(
            {
                "time": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "title": str(
                    row.get("event") or row.get("title") or "economic release"
                ),
                "impact": "high",
                "country": country,
                "markets": _markets_for_country(country),
                "source": "finnhub",
                "actual": row.get("actual"),
                "estimate": row.get("estimate"),
                "prev": row.get("prev"),
            }
        )
    events.sort(key=lambda e: e["time"])
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Finnhub economic calendar")
    parser.add_argument("--days", type=int, default=7, help="Forward days to fetch")
    args = parser.parse_args()

    key = finnhub_api_key()
    if not key or key in _PLACEHOLDER_KEYS or key.lower().startswith("your_"):
        print(
            "No valid Finnhub key — edit config/external_keys.json "
            "(finnhub_api_key) or export FINNHUB_API_KEY",
            file=sys.stderr,
        )
        return 1

    try:
        events = fetch_economic_calendar(api_key=key, days=args.days)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        print(
            f"Finnhub API error {status} — check key in {_key_source_hint()}",
            file=sys.stderr,
        )
        if status == 401:
            print(
                "401 Unauthorized: key is missing, placeholder, or invalid. "
                "If you set FINNHUB_API_KEY in the shell, it overrides the JSON file.",
                file=sys.stderr,
            )
        return 1
    out_dir = ROOT / "data_lake" / "external"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_forward": args.days,
        "count": len(events),
        "events": events,
    }
    out_path = out_dir / "finnhub_economic_calendar.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Finnhub: {len(events)} high-impact events → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
