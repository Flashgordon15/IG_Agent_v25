#!/usr/bin/env python3
"""
Staging Feature Controller — zero-reboot runtime flag toggling.

Leverages the existing ConfigLoader merge chain:
  - update_config_values()   → persistent (disk write + singleton refresh)
  - apply_runtime_overrides() → in-memory soak (non-persistent)

On toggle-off of stateful features, safely clears the associated module-level
state dicts under their respective locks to prevent stale data leaks.

Usage:
    PYTHONPATH=src python3 scripts/staging_feature_controller.py status
    PYTHONPATH=src python3 scripts/staging_feature_controller.py toggle volatility_bracket_enabled
    PYTHONPATH=src python3 scripts/staging_feature_controller.py soak volatility_bracket_enabled false
    PYTHONPATH=src python3 scripts/staging_feature_controller.py reset-state
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

TOGGLEABLE_FLAGS = (
    "volatility_bracket_enabled",
    "adaptive_trailing_stop_enabled",
    "capital_recycle_enabled",
    "enforce_top3_rotation_filter",
    "enforce_rr_floor_filter",
    "enforce_1h_ema_filter",
    "enforce_environment_fitness_filter",
    "ml_filter_overrides_enabled",
    "trading_hours_enabled",
)

_STATE_RESET_MAP: dict[str, list[str]] = {
    "volatility_bracket_enabled": [
        "execution.risk_manager.reset_volatility_bracket_for_tests",
    ],
    "adaptive_trailing_stop_enabled": [
        "execution.risk_manager.reset_asymmetric_risk_for_tests",
    ],
}


def _get_config():
    from system.config_loader import get_config
    return get_config()


def _read_flag(cfg, flag: str) -> Any:
    if hasattr(cfg, flag):
        return getattr(cfg, flag)
    return cfg.as_dict().get(flag, "<unset>")


def _resolve_callable(dotpath: str):
    mod_path, func_name = dotpath.rsplit(".", 1)
    mod = __import__(mod_path, fromlist=[func_name])
    return getattr(mod, func_name)


def _clear_state_for_flag(flag: str) -> list[str]:
    """Call reset functions associated with a flag. Returns names of functions called."""
    callables = _STATE_RESET_MAP.get(flag, [])
    called = []
    for dotpath in callables:
        fn = _resolve_callable(dotpath)
        fn()
        called.append(dotpath.rsplit(".", 1)[1])
    return called


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = _get_config()
    data = cfg.as_dict()
    print("=== Staging Feature Controller — Flag Status ===")
    print(f"  Config source: {data.get('$extends', 'primary')}")
    print(f"  Timestamp:     {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    max_w = max(len(f) for f in TOGGLEABLE_FLAGS)
    for flag in TOGGLEABLE_FLAGS:
        val = _read_flag(cfg, flag)
        marker = "ON" if val else "OFF"
        print(f"  {flag:<{max_w}}  {marker:>3}  ({val})")
    print()
    from execution.risk_manager import (
        _volatility_bracket_states,
        _volatility_bracket_last,
        _tick_highs,
    )
    print("  State dict sizes:")
    print(f"    _volatility_bracket_states: {len(_volatility_bracket_states)} entries")
    print(f"    _volatility_bracket_last:   {len(_volatility_bracket_last)} entries")
    print(f"    _tick_highs (asymmetric):    {len(_tick_highs)} entries")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    flag = args.flag
    if flag not in TOGGLEABLE_FLAGS:
        print(f"ERROR: '{flag}' is not a toggleable flag.", file=sys.stderr)
        print(f"  Allowed: {', '.join(TOGGLEABLE_FLAGS)}", file=sys.stderr)
        return 1

    cfg_before = _get_config()
    old_val = bool(_read_flag(cfg_before, flag))
    new_val = not old_val

    from system.config_loader import update_config_values
    update_config_values(**{flag: new_val})

    print(f"[TOGGLE] {flag}: {old_val} -> {new_val}  (persistent, disk-written)")

    if not new_val:
        cleared = _clear_state_for_flag(flag)
        if cleared:
            print(f"  State cleared: {', '.join(cleared)}")
    return 0


def cmd_soak(args: argparse.Namespace) -> int:
    flag = args.flag
    raw = args.value.lower()
    if raw in ("true", "1", "yes", "on"):
        val = True
    elif raw in ("false", "0", "no", "off"):
        val = False
    else:
        print(f"ERROR: value must be true/false, got '{args.value}'", file=sys.stderr)
        return 1

    if flag not in TOGGLEABLE_FLAGS:
        print(f"ERROR: '{flag}' is not a toggleable flag.", file=sys.stderr)
        return 1

    from system.config_loader import apply_runtime_overrides
    apply_runtime_overrides(**{flag: val})

    print(f"[SOAK] {flag} = {val}  (in-memory only, not persisted to disk)")

    if not val:
        cleared = _clear_state_for_flag(flag)
        if cleared:
            print(f"  State cleared: {', '.join(cleared)}")
    return 0


def cmd_reset_state(_args: argparse.Namespace) -> int:
    print("[RESET] Clearing all stateful risk dicts...")
    for flag, callables in _STATE_RESET_MAP.items():
        for dotpath in callables:
            fn = _resolve_callable(dotpath)
            fn()
            print(f"  {dotpath.rsplit('.', 1)[1]}() called")
    print("  All state dicts cleared.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Staging Feature Controller — zero-reboot runtime flag toggling"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current feature flag states")

    p_toggle = sub.add_parser("toggle", help="Flip a boolean flag persistently")
    p_toggle.add_argument("flag", help="Config flag name to toggle")

    p_soak = sub.add_parser("soak", help="Set a flag in-memory (non-persistent)")
    p_soak.add_argument("flag", help="Config flag name")
    p_soak.add_argument("value", help="true or false")

    sub.add_parser("reset-state", help="Clear all stateful risk dicts")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 1

    dispatch = {
        "status": cmd_status,
        "toggle": cmd_toggle,
        "soak": cmd_soak,
        "reset-state": cmd_reset_state,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
