"""
Percentage profit tiers per market — bank at 5%, 7.5%, 10%, … of profit target.

Tiers are expressed as % of the position's GBP profit target (not account %).
Larger tiers (configurable, default ≥25%) can defer banking when sentiment,
news timing, and hold duration favour letting the runner extend.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

_PCT_LABEL_RE = re.compile(r"pct[_-]?(\d+(?:\.\d+)?)", re.I)


@dataclass(frozen=True)
class ProfitPctTier:
    pct: float
    peak_min_gbp: float
    bank_floor_gbp: float
    fade_ratio: float
    label: str


@dataclass(frozen=True)
class PctTierDecision:
    reason: str
    tier_pct: float
    peak_pct_of_target: float
    profit_pct_of_target: float
    runner_extended: bool = False


def _cfg_block(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    raw = getattr(cfg, "profit_pct_tiers", None)
    if raw is None and hasattr(cfg, "get"):
        raw = cfg.get("profit_pct_tiers")
    return dict(raw) if isinstance(raw, dict) else {}


def pct_tiers_enabled(cfg: Any | None) -> bool:
    block = _cfg_block(cfg)
    return bool(block.get("enabled", False))


def _fade_ratio_for_pct(pct: float, row: dict[str, Any] | None = None) -> float:
    if row and row.get("fade_ratio") is not None:
        return max(0.35, min(0.90, float(row["fade_ratio"])))
    return max(0.45, min(0.85, 0.48 + (float(pct) / 100.0) * 0.32))


def _floor_ratio_for_pct(pct: float, fade: float, row: dict[str, Any] | None) -> float:
    if row and row.get("floor_ratio") is not None:
        return max(0.30, min(0.90, float(row["floor_ratio"])))
    return max(0.40, fade - 0.06)


def _tier_rows_for_epic(epic: str, cfg: Any | None) -> list[dict[str, Any]]:
    block = _cfg_block(cfg)
    per = block.get("per_epic")
    epic_rows: list[dict[str, Any]] | None = None
    if isinstance(per, dict):
        epic_cfg = per.get(str(epic or "").strip())
        if isinstance(epic_cfg, dict):
            raw = epic_cfg.get("tiers")
            if isinstance(raw, list) and raw:
                epic_rows = [r for r in raw if isinstance(r, dict)]
    if epic_rows:
        return epic_rows
    default = block.get("default_tiers")
    if isinstance(default, list) and default:
        if default and isinstance(default[0], dict):
            return [r for r in default if isinstance(r, dict)]
        return [{"pct": float(p)} for p in default if p is not None]
    return [
        {"pct": 5.0},
        {"pct": 7.5},
        {"pct": 10.0},
        {"pct": 15.0},
        {"pct": 25.0},
        {"pct": 50.0},
        {"pct": 75.0},
        {"pct": 100.0},
    ]


def build_pct_tiers(
    *,
    epic: str,
    target_gbp: float,
    cfg: Any | None = None,
) -> tuple[ProfitPctTier, ...]:
    """Materialise GBP thresholds from % of profit target."""
    target = max(0.5, float(target_gbp or 0))
    rows = _tier_rows_for_epic(epic, cfg)
    tiers: list[ProfitPctTier] = []
    for row in rows:
        try:
            pct = float(row.get("pct") or 0)
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            continue
        peak_min = target * (pct / 100.0)
        fade = _fade_ratio_for_pct(pct, row)
        floor_r = _floor_ratio_for_pct(pct, fade, row)
        bank_floor = peak_min * floor_r
        label = str(row.get("label") or f"pct_{pct:g}")
        tiers.append(
            ProfitPctTier(
                pct=pct,
                peak_min_gbp=round(peak_min, 4),
                bank_floor_gbp=round(bank_floor, 4),
                fade_ratio=fade,
                label=label,
            )
        )
    return tuple(sorted(tiers, key=lambda t: t.pct))


def _runner_block(cfg: Any | None) -> dict[str, Any]:
    block = _cfg_block(cfg)
    ext = block.get("runner_extension")
    return dict(ext) if isinstance(ext, dict) else {}


def capture_exit_context(*, epic: str, direction: str) -> dict[str, float]:
    """Sentiment + news timing snapshot at exit evaluation (non-blocking)."""
    key = str(epic or "").strip()
    direction_u = str(direction or "BUY").upper()
    ctx: dict[str, float] = {
        "sentiment_delta_5m": 0.0,
        "sentiment_surface": 0.0,
        "news_countdown_norm": 0.0,
        "headline_accel": 0.0,
    }
    try:
        from trading.sentiment_momentum import sentiment_momentum_features

        sent = sentiment_momentum_features(key)
        ctx["sentiment_delta_5m"] = float(sent.get("delta_5m") or 0.0)
        ctx["sentiment_surface"] = float(sent.get("surface_score") or 0.0)
    except Exception:
        pass
    try:
        from trading.probability_engine import compile_cognitive_reasoning

        cog = compile_cognitive_reasoning(epic=key)
        ctx["news_countdown_norm"] = float(cog.get("news_countdown_norm") or 0.0)
    except Exception:
        pass
    try:
        from system.market_data_hub import get_headline_urgency_snapshot

        recent = get_headline_urgency_snapshot().get("epics", {}).get(key) or {}
        ctx["headline_accel"] = float(recent.get("acceleration") or 0.0)
    except Exception:
        pass
    # Direction alignment score: positive when flow supports the open side.
    align = ctx["sentiment_delta_5m"]
    if direction_u == "SELL":
        align = -align
    ctx["sentiment_align"] = align
    return ctx


def runner_should_extend(
    *,
    epic: str,
    direction: str,
    peak_pct_of_target: float,
    armed_at: float,
    cfg: Any | None = None,
) -> bool:
    """
    For larger % peaks, defer tier banking when sentiment/news/timing favour holding.
    """
    ext = _runner_block(cfg)
    if not ext.get("enabled", True):
        return False
    min_pct = float(ext.get("min_pct", 25.0))
    if peak_pct_of_target < min_pct:
        return False
    max_skip = float(ext.get("max_pct_skip_bank", 85.0))
    if peak_pct_of_target > max_skip:
        return False
    min_hold = float(ext.get("min_hold_sec", 45.0))
    if armed_at > 0 and (time.time() - armed_at) < min_hold:
        return False

    ctx = capture_exit_context(epic=epic, direction=direction)
    news_max = float(ext.get("news_countdown_max", 0.40))
    if ctx["news_countdown_norm"] >= news_max:
        return False

    if ext.get("sentiment_align_required", True):
        min_align = float(ext.get("sentiment_delta_min", 0.0))
        if ctx["sentiment_align"] < min_align:
            return False

    if ext.get("require_news_clear", False) and ctx["news_countdown_norm"] > 0.15:
        return False

    if ext.get("require_headline_momentum", False):
        if abs(ctx["headline_accel"]) < float(ext.get("headline_accel_min", 0.0005)):
            return False

    try:
        if ext.get("use_regime_alpha", True):
            from trading.probability_engine import sentiment_regime_alpha_aligned

            if not sentiment_regime_alpha_aligned(epic):
                return False
    except Exception:
        pass

    return True


def pct_tier_bank_reason(
    *,
    peak: float,
    pnl: float,
    target_gbp: float,
    epic: str,
    direction: str,
    trail_trigger_gbp: float,
    armed_at: float = 0.0,
    cfg: Any | None = None,
) -> PctTierDecision | None:
    """
    Return tier bank decision when a % tier fade fires, else None.

    Below trail_trigger the % tiers own micro banks; above trail_trigger the
    trail floor owns exits unless runner extension defers a high-tier bank.
    """
    if not pct_tiers_enabled(cfg):
        return None
    if peak <= 0 or pnl <= 0:
        return None

    target = max(0.5, float(target_gbp or 0))
    peak_pct = (peak / target) * 100.0
    profit_pct = (pnl / target) * 100.0
    trigger = float(trail_trigger_gbp or 0)

    tiers = build_pct_tiers(epic=epic, target_gbp=target, cfg=cfg)
    if not tiers:
        return None

    for tier in reversed(tiers):
        if peak < tier.peak_min_gbp:
            continue
        if trigger > 0 and peak >= trigger and tier.pct < float(
            _runner_block(cfg).get("min_pct", 25.0)
        ):
            return None
        fade_level = peak * tier.fade_ratio
        if pnl > fade_level or pnl < tier.bank_floor_gbp:
            continue
        if runner_should_extend(
            epic=epic,
            direction=direction,
            peak_pct_of_target=peak_pct,
            armed_at=armed_at,
            cfg=cfg,
        ):
            return PctTierDecision(
                reason=(
                    f"runner_extend defer_{tier.label} pnl={pnl:.2f} "
                    f"peak={peak:.2f} peak_pct={peak_pct:.1f}%"
                ),
                tier_pct=tier.pct,
                peak_pct_of_target=round(peak_pct, 2),
                profit_pct_of_target=round(profit_pct, 2),
                runner_extended=True,
            )
        return PctTierDecision(
            reason=(
                f"{tier.label} pnl={pnl:.2f} peak={peak:.2f} "
                f"fade<={fade_level:.2f} floor>={tier.bank_floor_gbp:.2f} "
                f"tier_pct={tier.pct:g}%"
            ),
            tier_pct=tier.pct,
            peak_pct_of_target=round(peak_pct, 2),
            profit_pct_of_target=round(profit_pct, 2),
            runner_extended=False,
        )
    return None


def classify_tier_pct_from_reason(exit_reason: str) -> float | None:
    """Extract tier % label from exit reason for ML bucketing."""
    text = str(exit_reason or "")
    m = _PCT_LABEL_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    if "runner_extend" in text:
        m2 = re.search(r"defer_pct[_-]?(\d+(?:\.\d+)?)", text, re.I)
        if m2:
            try:
                return float(m2.group(1))
            except ValueError:
                pass
    return None


def assess_profit_tier_strategy(
    closes: list[dict[str, Any]],
    *,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Summarise WR/PnL by profit tier % for strategy review."""
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for row in closes:
        pct = row.get("profit_tier_pct")
        if pct is None:
            pct = classify_tier_pct_from_reason(str(row.get("exit_reason") or ""))
        key = f"{float(pct):g}%" if pct is not None else "unclassified"
        by_tier.setdefault(key, []).append(row)

    tiers_out: dict[str, Any] = {}
    for key, rows in sorted(by_tier.items(), key=lambda kv: kv[0]):
        n = len(rows)
        wins = sum(1 for r in rows if r.get("won"))
        total = sum(float(r.get("pnl_gbp") or 0) for r in rows)
        extended = sum(1 for r in rows if r.get("runner_extended"))
        tiers_out[key] = {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "total_pnl_gbp": round(total, 2),
            "avg_pnl_gbp": round(total / n, 2) if n else 0.0,
            "runner_extended": extended,
        }

    ext = _runner_block(cfg)
    return {
        "enabled": pct_tiers_enabled(cfg),
        "reference": _cfg_block(cfg).get("reference", "target"),
        "runner_extension": ext,
        "by_tier_pct": tiers_out,
    }
