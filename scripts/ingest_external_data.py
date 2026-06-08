#!/usr/bin/env python3
"""
Pull macro series into data_lake/external/ for v26 research (no live REST budget).

  PYTHONPATH=src python3 scripts/ingest_external_data.py
  PYTHONPATH=src python3 scripts/ingest_external_data.py --days 90
  PYTHONPATH=src python3 scripts/ingest_external_data.py --fx   # Alpha Vantage FX ref
  PYTHONPATH=src python3 scripts/ingest_external_data.py --calendar  # Finnhub econ
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Yahoo symbols for cross-asset regime features
MACRO_SYMBOLS = {
    "dxy": "DX-Y.NYB",
    "vix": "^VIX",
    "us10y": "^TNX",
    "gold_spot": "GC=F",
    "wti": "CL=F",
}

FX_PAIRS_AV = {
    "eurusd": ("EUR", "USD"),
    "gbpusd": ("GBP", "USD"),
}


def _fetch_yahoo(symbol: str, *, days: int) -> list[dict]:
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed — pip install yfinance", file=sys.stderr)
        return []
    period = f"{max(days, 7)}d"
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return []
    rows: list[dict] = []
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        rows.append(
            {
                "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": float(row.get("Open", 0)),
                "h": float(row.get("High", 0)),
                "l": float(row.get("Low", 0)),
                "c": float(row.get("Close", 0)),
                "v": float(row.get("Volume", 0) or 0),
            }
        )
    return rows


def _fetch_alphavantage_fx(from_ccy: str, to_ccy: str, *, api_key: str) -> dict | None:
    import requests

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_DAILY",
        "from_symbol": from_ccy,
        "to_symbol": to_ccy,
        "outputsize": "compact",
        "apikey": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "Time Series FX (Daily)" not in data:
            return None
        return data
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest external macro OHLC")
    parser.add_argument("--days", type=int, default=60, help="History depth")
    parser.add_argument(
        "--fx",
        action="store_true",
        help="Also pull Alpha Vantage FX daily (needs ALPHAVANTAGE_API_KEY)",
    )
    parser.add_argument(
        "--calendar",
        action="store_true",
        help="Also pull Finnhub economic calendar (needs FINNHUB_API_KEY)",
    )
    args = parser.parse_args()

    out_root = ROOT / "data_lake" / "external"
    out_root.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest: dict[str, object] = {
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": args.days,
        "series": {},
    }

    if args.calendar:
        sys.path.insert(0, str(ROOT / "scripts"))
        from ingest_finnhub_calendar import fetch_economic_calendar

        from system.external_keys import finnhub_api_key

        fk = finnhub_api_key()
        if fk:
            events = fetch_economic_calendar(api_key=fk, days=7)
            cal_path = out_root / "finnhub_economic_calendar.json"
            cal_path.write_text(
                json.dumps(
                    {
                        "fetched_at": manifest["ingested_at"],
                        "days_forward": 7,
                        "count": len(events),
                        "events": events,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            manifest["finnhub_calendar"] = {
                "events": len(events),
                "path": str(cal_path),
            }
            print(f"finnhub_calendar: {len(events)} events → {cal_path}")
        else:
            print("finnhub_calendar: skipped (no API key)", file=sys.stderr)

    if args.fx:
        from system.external_keys import alphavantage_api_key

        av_key = alphavantage_api_key()
        if av_key:
            for name, (fr, to) in FX_PAIRS_AV.items():
                data = _fetch_alphavantage_fx(fr, to, api_key=av_key)
                if data:
                    path = out_root / f"{name}_alphavantage_daily.json"
                    path.write_text(json.dumps(data), encoding="utf-8")
                    series = data.get("Time Series FX (Daily)") or {}
                    manifest["series"][name] = {
                        "source": "alphavantage",
                        "bars": len(series),
                        "path": str(path.relative_to(ROOT)),
                    }
                    print(f"{name}: {len(series)} AV bars → {path}")
                else:
                    print(f"{name}: Alpha Vantage fetch failed", file=sys.stderr)
        else:
            print("alphavantage fx: skipped (no API key)", file=sys.stderr)

    for name, symbol in MACRO_SYMBOLS.items():
        bars = _fetch_yahoo(symbol, days=args.days)
        path = out_root / f"{name}_daily.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for bar in bars:
                fh.write(json.dumps(bar) + "\n")
        manifest["series"][name] = {
            "symbol": symbol,
            "bars": len(bars),
            "path": str(path.relative_to(ROOT)),
        }
        print(f"{name}: {len(bars)} bars → {path}")

    meta_path = out_root / f"manifest_{day}.json"
    meta_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
