"""Replay S2/S3 on historical OHLC bars (months of learning without live fills)."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# v26 strategies only — no v25 SignalEngine
from strategies.s2_momentum import S2Momentum
from strategies.s3_session_fx import S3SessionFx


def _ensure_src_path() -> None:
    root = Path(__file__).resolve().parents[2]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _load_enabled_markets() -> list[tuple[str, str, str]]:
    """Return (instrument_id, epic, market_name) for enabled v25 instruments."""
    _ensure_src_path()
    from trading.instrument_registry import InstrumentRegistry

    root = Path(__file__).resolve().parents[2]
    cfg_path = root / "config" / "config_v25.json"
    if not cfg_path.is_file():
        return []
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    reg = InstrumentRegistry(raw)
    out: list[tuple[str, str, str]] = []
    for iid, inst in reg.get_enabled_with_ids():
        epic = str(inst.get("epic") or "")
        name = str(inst.get("name") or iid)
        if epic:
            out.append((iid, epic, name))
    return out


def _load_bars(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    bars: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            bars.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    bars.sort(key=lambda b: str(b.get("t") or ""))
    return bars


def _session_for_bar(bar_time: str) -> str:
    _ensure_src_path()
    try:
        from datetime import datetime

        from signals.indicators import session_name

        raw = str(bar_time or "").strip()
        if not raw:
            return ""
        if raw.endswith("Z"):
            raw = raw[:-1]
        dt = datetime.fromisoformat(raw)
        return str(session_name(dt) or "")
    except Exception:
        return ""


def _bar_to_event(
    bar: dict[str, Any], *, epic: str, market: str, session: str
) -> dict[str, Any]:
    return {
        "event_type": "bar_close",
        "ts": str(bar.get("t") or "") + "Z" if bar.get("t") else "",
        "epic": epic,
        "market": market,
        "session": session,
        "payload": {
            "bar_time": bar.get("t"),
            "open": bar.get("o"),
            "high": bar.get("h"),
            "low": bar.get("l"),
            "close": bar.get("c"),
            "volume": bar.get("v"),
        },
    }


def run_historical_bar_lab(
    *,
    max_bars_per_market: int = 0,
) -> dict[str, Any]:
    _ensure_src_path()
    from trading.ohlc_cache_paths import ohlc_cache_path

    s2 = S2Momentum()
    s3 = S3SessionFx()
    by_strategy: dict[str, dict[str, int]] = defaultdict(
        lambda: {"intents": 0, "would_trade": 0}
    )
    by_epic: dict[str, dict[str, int]] = defaultdict(
        lambda: {"s2_wt": 0, "s3_wt": 0, "bars": 0}
    )
    markets_out: list[dict[str, Any]] = []

    for iid, epic, market in _load_enabled_markets():
        cache = ohlc_cache_path(epic, market=market)
        bars = _load_bars(cache)
        if max_bars_per_market > 0:
            bars = bars[-max_bars_per_market:]
        fired_s2 = fired_s3 = 0
        for bar in bars:
            session = _session_for_bar(str(bar.get("t") or ""))
            event = _bar_to_event(bar, epic=epic, market=market, session=session)
            by_epic[epic]["bars"] += 1
            for strat in (s2, s3):
                intent = strat.evaluate_feeder_event(event)
                if intent is None:
                    continue
                sid = intent.strategy_id
                by_strategy[sid]["intents"] += 1
                if intent.would_trade:
                    by_strategy[sid]["would_trade"] += 1
                    if sid == "S2_momentum":
                        fired_s2 += 1
                        by_epic[epic]["s2_wt"] += 1
                    elif sid == "S3_session_fx":
                        fired_s3 += 1
                        by_epic[epic]["s3_wt"] += 1
        markets_out.append(
            {
                "instrument": iid,
                "epic": epic,
                "market": market,
                "bars": len(bars),
                "cache": str(cache),
                "s2_would_trade": fired_s2,
                "s3_would_trade": fired_s3,
            }
        )

    total_bars = sum(m["bars"] for m in markets_out)
    return {
        "ok": total_bars > 0,
        "markets": len(markets_out),
        "total_bars": total_bars,
        "by_strategy": dict(by_strategy),
        "by_epic": dict(by_epic),
        "markets_detail": markets_out,
    }
