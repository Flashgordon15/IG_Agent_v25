#!/usr/bin/env python3
"""Print deal-size and ATR stop calculations per enabled instrument (IG DEMO)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.adaptive_engine import AdaptiveEngine
from execution.risk_manager import RiskManager
from runtime.agent_bootstrap import _config_for_instrument
from system.config_loader import ConfigLoader
from system.credentials_loader import try_load_credentials
from system.ig_rest_session import ensure_shared_authenticated
from trading.instrument_registry import InstrumentRegistry


def _sample_atr(epic: str) -> float:
    """Representative 5m ATR from OHLC cache tail (price units)."""
    from trading.ohlc_cache_paths import ohlc_cache_path

    reg_path = ROOT / "config" / "config_v25.json"
    raw = json.loads(reg_path.read_text(encoding="utf-8"))
    inst = InstrumentRegistry(raw).get_by_epic(epic) or {}
    market = str(inst.get("name") or epic)
    path = ohlc_cache_path(epic, market=market)
    if not path.is_file():
        return 0.0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) < 20:
        return 0.0
    import pandas as pd

    from signals.indicators import atr

    rows = []
    for ln in lines[-120:]:
        try:
            b = json.loads(ln)
            rows.append(
                {
                    "high": float(b.get("h") or b.get("c") or 0),
                    "low": float(b.get("l") or b.get("c") or 0),
                    "close": float(b.get("c") or 0),
                }
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    if len(rows) < 15:
        return 0.0
    df = pd.DataFrame(rows)
    series = atr(df, 14)
    val = float(series.iloc[-1]) if len(series) else 0.0
    return val if val > 0 else 0.0


def main() -> int:
    cfg_path = ROOT / "config" / "config_v25.json"
    base_cfg = ConfigLoader(cfg_path).load_config()
    reg = InstrumentRegistry(json.loads(cfg_path.read_text(encoding="utf-8")))

    rest = None
    cred = try_load_credentials()
    if cred.ok and cred.credentials:
        try:
            rest = ensure_shared_authenticated(cred.credentials)
        except Exception as exc:
            print(f"IG session unavailable: {exc}")

    print("=== INSTRUMENT SIZING & ATR STOP VERIFICATION ===\n")
    for iid, inst in reg.get_enabled_with_ids():
        epic = str(inst.get("epic") or "")
        name = str(inst.get("name") or iid)
        loop_cfg = _config_for_instrument(base_cfg, inst)
        adaptive = AdaptiveEngine(loop_cfg)
        risk_mgr = RiskManager(loop_cfg)

        atr_sample = _sample_atr(epic)
        snapshot = {"last": {"atr": atr_sample, "spread": 1.0}}
        settings = adaptive.settings("demo_setup", 85.0, snapshot)
        risk_assess = risk_mgr.assess(
            direction="BUY",
            execution_params=settings,
            account_balance=10000.0,
            account_available=9500.0,
        )

        print(f"## {name} ({epic})")
        print(f"  Config trade_size:        {loop_cfg.trade_size}")
        print(f"  Config stop_distance:     {loop_cfg.stop_distance_points}")
        print(f"  ATR risk clamp:           {loop_cfg.adaptive_min_risk_points} – {loop_cfg.adaptive_max_risk_points}")
        print(f"  ATR multiplier:           {loop_cfg.atr_multiplier}")
        print(f"  ig_point_value_gbp:       {loop_cfg.get('ig_point_value_gbp', 1.0)}")
        print(f"  Sample 5m ATR (cache):    {atr_sample:.6g}")
        print(f"  Adaptive size (pre-IG):   {settings['size']}")
        print(f"  Adaptive stop (pre-IG):   {settings['risk']:.4g}  ({settings.get('notes', '')})")
        print(f"  RiskManager size/stop:    {risk_assess.size} / {risk_assess.stop_distance:.4g}")

        if rest is not None:
            try:
                norm_size, norm_stop, norm_limit, ccy = rest.normalize_order_params(
                    epic,
                    size=risk_assess.size,
                    stop_distance=risk_assess.stop_distance,
                    limit_distance=risk_assess.limit_distance,
                    currency_code=str(loop_cfg.currency_code),
                )
                c = rest.fetch_market_constraints(epic)
                print(f"  IG min_deal_size:         {c['min_deal_size']}")
                print(f"  IG min_stop_distance:     {c['min_stop_distance']}")
                print(f"  IG currency:              {c['currency_code']}")
                print(f"  Final deal size:          {norm_size}")
                print(f"  Final stop distance:      {norm_stop:.4g}")
                print(f"  Final limit distance:     {norm_limit}")
                print(f"  Risk £ (size×stop×£/pt):  £{norm_size * norm_stop * float(loop_cfg.get('ig_point_value_gbp', 1.0)):.2f}")
            except Exception as exc:
                print(f"  IG normalize:             ERROR {exc}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
