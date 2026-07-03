"""Lock-free Flight Deck payload sanitizer — corrupt API JSON → safe UI defaults."""

from __future__ import annotations

from typing import Any

UI_BASELINE_PP = 1000
UI_BASELINE_EXPOSURE_GBP = 0.0
UI_BASELINE_TRAJECTORY_TREND = "neutral"
UI_BASELINE_HEADLINE_URGENCY = 0.0
UI_BASELINE_COMPRESSION = 1.0

_BOOT_STAGE_IDS: tuple[str, ...] = (
    "STAGE_1_CONFIG_SANITY",
    "STAGE_2_GUARDIAN_WAKE",
    "STAGE_3_REGIME_HYDRATION",
    "STAGE_4_TUNER_PRIME",
    "STAGE_5_LAUNCH_CORE",
    "STAGE_6_REST_AUTH",
    "STAGE_7_STREAM_HANDSHAKE",
    "STAGE_8_DATA_FEED_HYDRATION",
    "STAGE_9_ALPHAS_ARMED",
)


def _safe_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if n == n else default  # NaN guard
    except (TypeError, ValueError):
        return default


def sanitize_pp_trajectory(raw: Any) -> dict[str, Any]:
    body = _safe_dict(raw)
    scores = [int(_safe_num(x, UI_BASELINE_PP)) for x in _safe_list(body.get("pp_scores"))]
    days = [str(x) for x in _safe_list(body.get("days"))]
    trend = str(body.get("trend") or UI_BASELINE_TRAJECTORY_TREND)
    if trend not in ("expansion", "defense", "neutral"):
        trend = UI_BASELINE_TRAJECTORY_TREND
    return {
        "ok": bool(body.get("ok", scores)),
        "pp_scores": scores,
        "days": days,
        "trend": trend,
        "slope": _safe_num(body.get("slope")),
        "latest_pp": int(_safe_num(body.get("latest_pp"), UI_BASELINE_PP)),
        "defense_threshold": int(_safe_num(body.get("defense_threshold"), 800)),
        "expansion_threshold": int(_safe_num(body.get("expansion_threshold"), 1200)),
    }


def sanitize_orchestrator_for_ui(raw: Any) -> dict[str, Any]:
    orch = _safe_dict(raw)
    sb = _safe_dict(orch.get("scoreboard"))
    tokens = _safe_dict(orch.get("stage_tokens"))
    status = _safe_dict(orch.get("stage_status") or orch.get("phase_status"))
    errors = {str(k): str(v)[:240] for k, v in _safe_dict(orch.get("stage_errors")).items()}
    return {
        "ok": bool(orch.get("ok", True)),
        "trade_ready": bool(orch.get("trade_ready")),
        "scoreboard": {
            "total_pp": int(_safe_num(sb.get("total_pp"), UI_BASELINE_PP)),
            "rank": str(sb.get("rank") or "standard"),
            "rolling_win_rate": _safe_num(sb.get("rolling_win_rate")),
            "capacity_multiplier": _safe_num(sb.get("capacity_multiplier"), 1.0),
            "size_factor_multiplier": _safe_num(sb.get("size_factor_multiplier"), 1.0),
        },
        "stage_tokens": {s: str(tokens.get(s) or "") for s in _BOOT_STAGE_IDS},
        "stage_status": {s: str(status.get(s) or "PENDING") for s in _BOOT_STAGE_IDS},
        "stage_errors": errors,
        "optimization": _safe_dict(orch.get("optimization")),
    }


def sanitize_ai_diagnostics_for_ui(raw: Any) -> dict[str, Any]:
    diag = _safe_dict(raw)
    ps = _safe_dict(diag.get("portfolio_synthesis"))
    news_alpha = _safe_dict(ps.get("news_alpha"))
    headlines = _safe_dict(news_alpha.get("headlines"))
    return {
        "ok": bool(diag.get("ok", True)),
        "synthetic_hydration_active": bool(diag.get("synthetic_hydration_active")),
        "fallback_transport_tier": str(diag.get("fallback_transport_tier") or ""),
        "portfolio_synthesis": {
            "covariance": _safe_dict(ps.get("covariance")),
            "cognitive_risk_heatmap": _safe_dict(ps.get("cognitive_risk_heatmap")),
            "news_alpha": {
                "headlines": headlines,
                "api_ingest": _safe_dict(news_alpha.get("api_ingest")),
            },
        },
        "pp_trajectory_7d": sanitize_pp_trajectory(diag.get("pp_trajectory_7d")),
    }


def sanitize_recovery_payload(
    *,
    orch: Any = None,
    diag: Any = None,
    iron: Any = None,
) -> dict[str, Any]:
    return {
        "orchestrator": sanitize_orchestrator_for_ui(orch),
        "diagnostics": sanitize_ai_diagnostics_for_ui(diag),
        "iron": _safe_dict(iron),
    }


def baseline_ui_snapshot() -> dict[str, Any]:
    """Empty / cold-start Iron Ledger substitute."""
    return {
        "ok": True,
        "platform_pp": UI_BASELINE_PP,
        "exposure_gbp": UI_BASELINE_EXPOSURE_GBP,
        "pp_trajectory_7d": sanitize_pp_trajectory({}),
        "scoreboard": {"total_pp": UI_BASELINE_PP},
    }
