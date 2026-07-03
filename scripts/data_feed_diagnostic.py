#!/usr/bin/env python3
"""Print data-feed hierarchy, primary feed, IG-on-signal check, multi-market readiness."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def _fetch_live_feed_state(timeout_sec: float = 2.0) -> dict | None:
    """Prefer the running agent's in-process hub when :8080 is live."""
    url = "http://127.0.0.1:8080/api/data_feed_state"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def main() -> int:
    live_state = _fetch_live_feed_state()
    if live_state is not None:
        print("=== IG Agent Data Feed Diagnostic (live API) ===")
        print(f"health: {live_state.get('health')}")
        print(f"primary_feed: {live_state.get('primary_feed') or 'none'}")
        print(f"fallback_active: {live_state.get('fallback_active')}")
        print(
            f"fresh_quotes: {live_state.get('fresh_count')}/{live_state.get('total_epics')}"
        )
        ig_on_signal_path = live_state.get("signal_path") not in (
            "yahoo_first",
            "",
            None,
        )
        try:
            from system.feeds.data_feed_orchestrator import ig_used_for_signal_path

            ig_violation = ig_used_for_signal_path()
        except Exception:
            ig_violation = ig_on_signal_path
        print(f"IG_on_signal_path: {ig_violation} (expect False)")
        print()
        print("Full state JSON:")
        print(json.dumps(live_state, indent=2, default=str))
        ok = (
            live_state.get("health") in ("ok", "degraded")
            and int(live_state.get("fresh_count") or 0) >= 1
            and not ig_violation
        )
        return 0 if ok else 1

    from system.feeds.data_feed_orchestrator import (
        get_data_feed_state,
        ig_used_for_signal_path,
    )
    from feeder.pricing_transport import reference_transport, reference_transport_is_yahoo
    from system.config_loader import ConfigLoader
    from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

    cfg = ConfigLoader().load(validate=False)
    state = get_data_feed_state()
    hub = get_market_data_hub()

    print("=== IG Agent Data Feed Diagnostic (offline) ===")
    print(f"reference_transport: {reference_transport(cfg)}")
    print(f"yahoo_primary: {reference_transport_is_yahoo(cfg)}")
    print(f"health: {state.get('health')}")
    print(f"primary_feed: {state.get('primary_feed') or 'none'}")
    print(f"fallback_active: {state.get('fallback_active')}")
    print(f"fresh_quotes: {state.get('fresh_count')}/{state.get('total_epics')}")
    print(f"IG_on_signal_path: {ig_used_for_signal_path()} (expect False)")
    print()
    print("Per-epic hub snapshot:")
    for epic in NIGHT_MATRIX_EPICS:
        snap = hub.get_snapshot(epic)
        if snap is None:
            print(f"  {epic}: missing")
            continue
        print(
            f"  {epic}: bid={snap.bid:.4f} age={snap.age_seconds():.1f}s "
            f"source={snap.source}"
        )
    print()
    print("Full state JSON:")
    print(json.dumps(state, indent=2, default=str))

    ok = (
        state.get("health") in ("ok", "degraded")
        and int(state.get("fresh_count") or 0) >= 1
        and not ig_used_for_signal_path()
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
