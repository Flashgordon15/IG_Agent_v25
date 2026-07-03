"""SRE cockpit snapshots — orchestrator, guardian, macro steering (Flight Deck REST)."""

from __future__ import annotations

import time
from typing import Any

_DEFAULT_MACRO_EPIC = "CS.D.CFPGOLD.CFP.IP"


def _default_macro_steering_epic() -> str:
    try:
        from system.market_data_hub import COCKPIT_CORE_EPICS

        if COCKPIT_CORE_EPICS:
            return str(COCKPIT_CORE_EPICS[0])
    except Exception:
        pass
    return _DEFAULT_MACRO_EPIC


def get_macro_steering_snapshot(epic: str | None = None) -> dict[str, Any]:
    """Aggregate macro radar, sentiment ROC, news countdown, and 48-bar shadow-walk."""
    key = str(epic or _default_macro_steering_epic()).strip()
    macro: dict[str, Any] = {}
    sentiment: dict[str, float] = {}
    news: dict[str, float] = {}
    shadow_walk: dict[str, Any] = {}
    sentiment_live = True
    news_live = True

    try:
        from intelligence.macro_radar import macro_radar_telemetry

        macro = macro_radar_telemetry()
    except Exception:
        pass

    try:
        from trading.sentiment_momentum import sentiment_momentum_features

        sentiment = sentiment_momentum_features(key)
    except Exception:
        sentiment_live = False
        sentiment = {
            "long_pct": 50.0,
            "delta_5m": 0.0,
            "delta_30m": 0.0,
            "surface_score": 0.0,
        }

    try:
        from system.calendar_gate import news_proximity_features

        news = news_proximity_features(key, use_cache=True)
    except Exception:
        news_live = False
        news = {
            "seconds_to_next": 86400.0,
            "countdown_norm": 0.0,
            "news_velocity": 0.0,
            "in_block_window": 0.0,
            "trailing_sensitivity_scale": 1.0,
        }

    try:
        from trading.probability_engine import run_48bar_shadow_walk_expectation

        direction = "BUY" if float(sentiment.get("delta_5m") or 0) >= 0 else "SELL"
        vector: list[float] = [0.0] * 128
        try:
            from signals.feature_state import compile_current_feature_state

            compiled = compile_current_feature_state(epic=key, market=key)
            raw_vec = compiled.get("vector")
            if raw_vec is not None and hasattr(raw_vec, "tolist"):
                vector = [float(x) for x in raw_vec.tolist()[:128]]
        except Exception:
            pass
        shadow_walk = run_48bar_shadow_walk_expectation(
            epic=key,
            direction=direction,
            feature_payload={"vector": vector},
        )
    except Exception:
        shadow_walk = {
            "projected_win_prob": None,
            "veto": False,
            "reason": "warming",
        }

    warming = not sentiment_live or not news_live or shadow_walk.get("projected_win_prob") is None

    walk_out = dict(shadow_walk) if isinstance(shadow_walk, dict) else {}
    prob = walk_out.get("projected_win_prob")
    floor = walk_out.get("veto_floor")
    if prob is not None and floor is not None:
        walk_out["summary"] = (
            f"Projected {float(prob) * 100:.1f}% win over 48 bars "
            f"(veto floor {float(floor) * 100:.0f}%)"
        )

    return {
        "ok": True,
        "epic": key,
        "macro": macro,
        "sentiment": sentiment,
        "news": news,
        "shadow_walk": walk_out,
        "data_quality": {
            "sentiment_live": sentiment_live,
            "news_live": news_live,
            "warming": warming,
        },
        "ts": time.time(),
    }
