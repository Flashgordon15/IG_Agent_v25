"""
Yahoo Finance OHLC fallback — free 5m history (max ~60 days) when IG is blocked.

CLI:
  PYTHONPATH=src python3 src/data/ohlc_yahoo_seeder.py
  PYTHONPATH=src python3 src/data/ohlc_yahoo_seeder.py --epic CS.D.EURUSD.CFD.IP
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from system.engine_log import log_engine
from system.paths import data_dir
from trading.ohlc_cache_paths import ohlc_cache_path

LONDON = ZoneInfo("Europe/London")
DEFAULT_INTERVAL = "5m"
DEFAULT_PERIOD = "60d"
MIN_BARS_REQUIRED = 100

# IG epic → (Yahoo symbol, display market name)
EPIC_YAHOO_MAP: dict[str, tuple[str, str]] = {
    "CS.D.EURUSD.CFD.IP": ("EURUSD=X", "EUR/USD"),
    "CS.D.CFPGOLD.CFP.IP": ("GC=F", "Spot Gold"),
    "IX.D.NIKKEI.IFM.IP": ("^N225", "Japan 225"),
    "CS.D.GBPUSD.CFD.IP": ("GBPUSD=X", "GBP/USD"),
    "CS.D.CRUDE.CFD.IP": ("CL=F", "US Oil WTI"),
    "IX.D.DOW.IFM.IP": ("^DJI", "Wall Street"),
}

DEFAULT_SEED_EPICS = (
    "CS.D.EURUSD.CFD.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.GBPUSD.CFD.IP",
    "CS.D.CRUDE.CFD.IP",
    "IX.D.DOW.IFM.IP",
)


def _price_decimals(yahoo_symbol: str) -> int:
    sym = yahoo_symbol.upper()
    if sym.endswith("=X") or "EUR" in sym:
        return 5
    if sym.startswith("^"):
        return 1
    return 2


def _default_spread(yahoo_symbol: str, close: float) -> float:
    sym = yahoo_symbol.upper()
    if sym.endswith("=X"):
        return 0.0002
    if sym == "GC=F":
        return 0.5
    return 15.0


def _iso_bar_time(dt: datetime) -> str:
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(LONDON).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _bars_from_dataframe(df: pd.DataFrame, yahoo_symbol: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    work = df.copy()
    work.index = pd.to_datetime(work.index)
    if work.index.tz is not None:
        work.index = work.index.tz_convert(LONDON).tz_localize(None)

    rename = {c: c.lower() for c in work.columns}
    work = work.rename(columns=str.lower)
    for col in ("open", "high", "low", "close"):
        if col not in work.columns:
            return []
    if "volume" not in work.columns:
        work["volume"] = 0.0

    ohlc = (
        work.resample("5min")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["close"])
    )

    decimals = _price_decimals(yahoo_symbol)
    bars: list[dict[str, Any]] = []
    for ts, row in ohlc.iterrows():
        o = float(row["open"])
        h = float(row["high"])
        low = float(row["low"])
        c = float(row["close"])
        if any(pd.isna(x) for x in (o, h, low, c)):
            continue
        spread = _default_spread(yahoo_symbol, c)
        bars.append(
            {
                "t": _iso_bar_time(ts),
                "o": round(o, decimals),
                "h": round(h, decimals),
                "l": round(low, decimals),
                "c": round(c, decimals),
                "v": round(float(row["volume"] or 0), 1),
                "spread": round(spread, decimals if decimals > 1 else 4),
                "source": "yahoo",
            }
        )
    bars.sort(key=lambda b: str(b["t"]))
    return bars


def validate_bars(bars: list[dict[str, Any]]) -> tuple[bool, str]:
    if len(bars) < MIN_BARS_REQUIRED:
        return False, f"too few bars ({len(bars)} < {MIN_BARS_REQUIRED})"
    prev_t = ""
    for i, bar in enumerate(bars):
        for key in ("t", "o", "h", "l", "c"):
            if bar.get(key) is None:
                return False, f"null field {key} at index {i}"
        o, h, low, c = float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"])
        if h < low or h < o or h < c or low > o or low > c:
            return False, f"invalid OHLC at {bar.get('t')}"
        t = str(bar["t"])
        if prev_t and t <= prev_t:
            return False, f"non-monotonic timestamp at {t}"
        prev_t = t
    return True, "ok"


def _write_bars(path: Path, bars: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(b, separators=(",", ":")) + "\n" for b in bars]
    path.write_text("".join(lines), encoding="utf-8")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fetch_yahoo_ohlc(
    symbol: str,
    interval: str,
    period: str,
    output_path: Path | str,
    *,
    overwrite: bool = True,
) -> int:
    """
    Download Yahoo history, resample to 5m, validate, and write JSONL cache.

    Returns number of bars written.
    """
    import yfinance as yf

    out = Path(output_path)
    log_engine(f"Yahoo OHLC fetch: symbol={symbol} interval={interval} period={period}")
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo returned no data for {symbol}")

    bars = _bars_from_dataframe(df, symbol)
    ok, reason = validate_bars(bars)
    if not ok:
        raise RuntimeError(f"Yahoo bar validation failed: {reason}")

    if not overwrite and out.is_file():
        existing: dict[str, dict[str, Any]] = {}
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                    t = str(row.get("t") or "")
                    if t:
                        existing[t] = row
                except json.JSONDecodeError:
                    continue
        for bar in bars:
            existing[str(bar["t"])] = bar
        bars = [existing[k] for k in sorted(existing)]

    _write_bars(out, bars)
    log_engine(f"Yahoo OHLC complete: {len(bars)} bars → {out}")
    return len(bars)


def fetch_yahoo_ohlc_for_epic(
    epic: str,
    market: str = "",
    *,
    interval: str = DEFAULT_INTERVAL,
    period: str = DEFAULT_PERIOD,
) -> int:
    """Resolve epic → Yahoo symbol and cache path, then fetch."""
    key = str(epic or "").strip()
    mapping = EPIC_YAHOO_MAP.get(key)
    if mapping is None:
        raise ValueError(f"No Yahoo mapping for epic {epic!r}")
    yahoo_symbol, _ = mapping
    cache_path = ohlc_cache_path(key, market=market or mapping[1])
    return fetch_yahoo_ohlc(
        yahoo_symbol,
        interval,
        period,
        cache_path,
        overwrite=True,
    )


def seed_default_instruments() -> dict[str, int]:
    """Seed EUR/USD and Gold (default CLI targets)."""
    results: dict[str, int] = {}
    for epic in DEFAULT_SEED_EPICS:
        market = EPIC_YAHOO_MAP[epic][1]
        count = fetch_yahoo_ohlc_for_epic(epic, market=market)
        results[epic] = count
        print(f"OK {market}: {count} bars → {ohlc_cache_path(epic, market=market)}")
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed OHLC cache from Yahoo Finance")
    parser.add_argument(
        "--epic",
        action="append",
        default=[],
        help="IG epic to seed (repeatable). Default: EUR/USD + Gold",
    )
    parser.add_argument("--symbol", default="", help="Yahoo symbol override")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--period", default=DEFAULT_PERIOD)
    parser.add_argument(
        "--market",
        default="",
        help="Market label for cache path when using --symbol",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.symbol:
        epic = str(args.epic[0] if args.epic else "").strip()
        path = ohlc_cache_path(epic, market=args.market) if epic else (
            data_dir() / "ohlc_cache" / "yahoo_custom_5m.jsonl"
        )
        count = fetch_yahoo_ohlc(
            args.symbol,
            args.interval,
            args.period,
            path,
        )
        print(f"Wrote {count} bars to {path}")
        return 0

    epics = list(args.epic) if args.epic else list(DEFAULT_SEED_EPICS)
    failed = 0
    for epic in epics:
        try:
            market = EPIC_YAHOO_MAP.get(epic, ("", epic))[1]
            count = fetch_yahoo_ohlc_for_epic(epic, market=market)
            print(f"OK {epic}: {count} bars")
        except Exception as e:
            failed += 1
            print(f"FAIL {epic}: {e}", file=sys.stderr)
            log_engine(f"Yahoo seed failed {epic}: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
