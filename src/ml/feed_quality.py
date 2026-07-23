"""Quote / feed quality scoring for ML entry decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from system.config import Config
from system.engine_log import log_engine


@dataclass
class FeedQualityVerdict:
    penalty_pts: float = 0.0
    veto: bool = False
    primary_feed: str = ""
    quote_age_sec: float = 0.0
    spread: float = 0.0
    reason: str = "ok"


def _feed_cfg(cfg: Config) -> dict[str, Any]:
    block = cfg.get("feed_quality")
    return dict(block) if isinstance(block, dict) else {}


def evaluate_feed_quality(
    cfg: Config,
    *,
    quote: Any | None = None,
    epic: str = "",
) -> FeedQualityVerdict:
    """Penalise stale or wide-spread quotes; veto when feed path is unhealthy."""
    fq = _feed_cfg(cfg)
    if not fq.get("enabled", True):
        return FeedQualityVerdict()

    max_age = float(fq.get("max_quote_age_sec") or 3.0)
    veto_age = float(fq.get("veto_quote_age_sec") or 8.0)
    max_spread = float(fq.get("max_spread_pts") or 0)  # 0 = skip spread check
    penalty_stale = float(fq.get("stale_penalty_pts") or 8.0)
    penalty_wide = float(fq.get("wide_spread_penalty_pts") or 6.0)

    quote_age = 0.0
    spread = 0.0
    primary = ""

    if quote is not None:
        try:
            spread = float(getattr(quote, "spread", 0) or 0)
            if spread <= 0:
                bid = float(getattr(quote, "bid", 0) or 0)
                offer = float(getattr(quote, "offer", 0) or 0)
                spread = max(0.0, offer - bid)
        except (TypeError, ValueError):
            spread = 0.0
        try:
            from datetime import datetime, timezone

            qt = getattr(quote, "time", None)
            if isinstance(qt, datetime):
                if qt.tzinfo is None:
                    qt = qt.replace(tzinfo=timezone.utc)
                quote_age = max(0.0, (datetime.now(timezone.utc) - qt).total_seconds())
        except Exception:
            quote_age = 0.0

    # Authoritative hub age when the loop quote lacks a trustworthy timestamp.
    if epic and quote_age <= 0.0:
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(str(epic))
            if snap is not None and snap.bid > 0 and snap.offer > 0:
                quote_age = float(snap.age_seconds())
        except Exception:
            pass

    try:
        from system.feeds.data_feed_orchestrator import get_data_feed_state

        snap = get_data_feed_state()
        primary = str(snap.get("primary_feed") or "")
        health = str(snap.get("health") or "")
        fresh = int(snap.get("fresh_count") or 0)
        total = int(snap.get("total_epics") or 0)
        if health not in ("ok", "degraded") or (total > 0 and fresh == 0):
            return FeedQualityVerdict(
                veto=True,
                primary_feed=primary,
                quote_age_sec=quote_age,
                spread=spread,
                reason=f"feed_health={health} fresh={fresh}/{total}",
            )
    except Exception:
        pass

    penalty = 0.0
    reason = "ok"

    if quote_age >= veto_age:
        log_engine(
            f"[FEED QUALITY] veto epic={epic[:24]} age={quote_age:.1f}s "
            f"primary={primary}"
        )
        return FeedQualityVerdict(
            veto=True,
            primary_feed=primary,
            quote_age_sec=quote_age,
            spread=spread,
            reason=f"quote_stale age={quote_age:.1f}s",
        )

    if quote_age > max_age:
        penalty += penalty_stale
        reason = f"stale age={quote_age:.1f}s"

    hard_spread = bool(fq.get("spread_hard_veto", True))
    if max_spread > 0 and spread > max_spread:
        if hard_spread:
            log_engine(
                f"[FEED QUALITY] spread hard veto epic={epic[:24]} "
                f"spread={spread:.1f}>{max_spread:.1f}"
            )
            return FeedQualityVerdict(
                veto=True,
                primary_feed=primary,
                quote_age_sec=quote_age,
                spread=spread,
                reason=f"spread_hard_veto {spread:.1f}>{max_spread:.1f}",
            )
        penalty += penalty_wide
        reason = f"{reason} spread={spread:.1f}>{max_spread:.1f}".strip()

    return FeedQualityVerdict(
        penalty_pts=penalty,
        primary_feed=primary,
        quote_age_sec=quote_age,
        spread=spread,
        reason=reason,
    )
