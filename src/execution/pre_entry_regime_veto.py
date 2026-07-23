"""Pre-entry regime veto — runs BEFORE any order generation or REST calls.

Pure local math on the instantaneous tick (bid/offer + OBI + spread elasticity
+ leader-follower proxy). Fail-closed on hard risks; may route to WORKING_ORDER
when the spread is elastically wide vs its 1-hour MA.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

DEFAULT_MAX_SPREAD_PCT = 0.0002  # 0.02% of mid

_TRENDING_REGIMES = frozenset(
    {"TREND", "HV_TREND", "BREAKOUT", "TREND_ACCELERATED"}
)
_BLOCKED_REGIMES = frozenset(
    {
        "RANGE_BOUND",
        "NEUTRAL",
        "CHOP",
        "MEAN_REVERSION",
        "RANGE",
        "RANGE_COMPRESSED",
        "LOW_VOL",
        "UNKNOWN",
        "STAGNANT_DZ",
        "STAGNANT",
        "DEAD_ZONE",
    }
)
_DOW_EPIC = "IX.D.DOW.IFM.IP"
SOVEREIGN_ML_THRESHOLD = 0.68
SOVEREIGN_OBI_MIN_ABS = 0.15

EntryRoute = Literal["MARKET", "WORKING_ORDER", "BLOCK"]


def _resolve_regime_label(epic: str) -> str:
    """Best-effort local regime label — never blocks on I/O failure alone."""
    key = str(epic or "").strip()
    try:
        from system.regime_state import get_regime_state_snapshot

        snap = get_regime_state_snapshot() or {}
        markets = snap.get("markets") or []
        if isinstance(markets, list):
            for row in markets:
                if not isinstance(row, dict):
                    continue
                if str(row.get("epic") or "") != key:
                    continue
                label = str(
                    row.get("state_label")
                    or row.get("regime_classification")
                    or row.get("label")
                    or ""
                ).upper()
                if label in ("MEAN_REVERSION", "HV_TREND", "CHOP"):
                    return label
                if label:
                    return label
                try:
                    st = int(row.get("state", -1))
                except (TypeError, ValueError):
                    st = -1
                if st == 1:
                    return "HV_TREND"
                if st == 2:
                    return "CHOP"
                if st == 0:
                    return "MEAN_REVERSION"
                gate = row.get("strategy_gate") or {}
                mode = str(gate.get("mode") or "").upper()
                if mode in ("REDUCED", "HALT", "CHOP"):
                    return "CHOP"
                if mode == "MOMENTUM":
                    return "HV_TREND"
                if mode == "FADE_EXTREMES":
                    return "MEAN_REVERSION"
    except Exception:
        pass
    return ""


def _normalize_sovereign_regime_label(raw: str) -> str:
    """Map regime_switch / scanner aliases to sovereign desk labels."""
    label = str(raw or "").strip().upper()
    if not label:
        return ""
    if label in ("MEAN_REVERSION", "RANGE", "RANGE_COMPRESSED", "STAGNANT_DZ", "STAGNANT", "DEAD_ZONE", "CHOP", "LOW_VOL"):
        return "RANGE_BOUND"
    if label in ("UNKNOWN", "WAIT", "MARGINAL"):
        return "NEUTRAL"
    return label


_INSTANT_VETO_LABELS = frozenset({"RANGE_BOUND", "NEUTRAL"})


def _sovereign_regime_label(epic: str) -> str:
    """Regime label from regime_state snapshot + strategy_gate (memory profile path)."""
    key = str(epic or "").strip()
    label = _normalize_sovereign_regime_label(_resolve_regime_label(key))
    if label in _INSTANT_VETO_LABELS:
        return label
    try:
        from system.regime_state import get_regime_state_snapshot

        snap = get_regime_state_snapshot() or {}
        markets = snap.get("markets") or []
        if isinstance(markets, list):
            for row in markets:
                if not isinstance(row, dict):
                    continue
                if str(row.get("epic") or "") != key:
                    continue
                raw = str(
                    row.get("state_label")
                    or row.get("regime_classification")
                    or row.get("label")
                    or ""
                )
                norm = _normalize_sovereign_regime_label(raw)
                if norm:
                    label = norm
                gate = row.get("strategy_gate") or {}
                mode = str(gate.get("mode") or "").upper()
                if not bool(gate.get("allow_entries", True)):
                    if mode in ("REDUCED", "HALT", "CHOP", "FADE_EXTREMES", ""):
                        return "NEUTRAL"
                    if mode not in ("MOMENTUM", "TREND", "BREAKOUT"):
                        return "NEUTRAL"
                break
    except Exception:
        pass
    return label


def evaluate_sovereign_regime_instant_block(
    epic: str,
    *,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """
    Purged — sovereign RANGE_BOUND / NEUTRAL instant veto no longer blocks entries.

    ML + OBI qualification bypasses trending-regime blocks instead (see
    ``_sovereign_ml_obi_bypass``). Kept for backward-compatible call sites.
    """
    _ = epic, cfg
    return True, "sovereign_instant_veto_purged"


def _resolve_bypass_obi_signal(
    epic: str,
    *,
    quote: Any | None = None,
) -> tuple[float, str]:
    """
    OBI conviction for sovereign bypass — align with sniper ML feature plane.

    Prefer microkernel OFI/OBI velocity (same source as QuantumSniperMLCore), then
    fall back to depthless quote-proxy ratio.
    """
    try:
        from apex.microkernel import get_microkernel
        from execution.entry_gate_hardening import resolve_raw_obi_ratio

        mt = get_microkernel().micro_trend_for(str(epic or ""))
        if isinstance(mt, dict):
            if mt.get("ofi_delta") is not None:
                v = abs(float(mt.get("ofi_delta") or 0.0))
                if v >= SOVEREIGN_OBI_MIN_ABS:
                    return v, "microkernel_ofi_delta"
            if mt.get("obi_ratio") is not None:
                v = abs(float(mt.get("obi_ratio") or 0.0))
                if v >= SOVEREIGN_OBI_MIN_ABS:
                    return v, "microkernel_obi_ratio"
    except Exception:
        pass
    obi, src = resolve_raw_obi_ratio(epic, quote=quote)
    return abs(float(obi)), src


def sovereign_ml_obi_bypass_qualifies(
    epic: str,
    direction: str,
    *,
    bid: float,
    offer: float,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """
    True when P(Success) >= 0.68 and |OBI| >= 0.15 on the sniper feature plane.

    Qualified sniper entries bypass MEAN_REVERSION / RANGE_BOUND / NEUTRAL / CHOP labels.
    Crash/melt-up opposing flow still fails bypass (downstream OBI guard remains).
    """
    if float(bid) <= 0 or float(offer) <= float(bid):
        return False, "sovereign_bypass_invalid_book"
    try:
        from types import SimpleNamespace

        from alpha.micro_sniper_ml import evaluate_live_sniper_probability
        from execution.entry_gate_hardening import resolve_raw_obi_ratio

        mid = (float(bid) + float(offer)) / 2.0
        quote = SimpleNamespace(bid=float(bid), offer=float(offer), mid=mid)
        result = evaluate_live_sniper_probability(
            epic, direction, cfg=cfg, quote=quote
        )
        p = float(result.p_success)
        if p < SOVEREIGN_ML_THRESHOLD:
            return (
                False,
                f"sovereign_bypass_ml_low p={p:.3f}<{SOVEREIGN_ML_THRESHOLD:.2f}",
            )
        min_abs = SOVEREIGN_OBI_MIN_ABS
        feat = getattr(result, "features", None)
        feat = feat if isinstance(feat, dict) else {}
        obi_vel = abs(float(feat.get("obi_velocity") or 0.0))
        raw_obi, raw_src = resolve_raw_obi_ratio(epic, quote=quote)
        sig_obi, sig_src = _resolve_bypass_obi_signal(epic, quote=quote)
        obi_mag = max(obi_vel, abs(float(raw_obi)), float(sig_obi))
        obi_src = raw_src if abs(float(raw_obi)) >= min_abs else sig_src
        if obi_vel >= min_abs:
            obi_src = "sniper_obi_velocity"
        if obi_mag < min_abs:
            return (
                False,
                f"sovereign_bypass_obi_low |obi|={obi_mag:.3f}<{min_abs:.2f} src={obi_src}",
            )
        dir_u = str(direction or "BUY").upper()
        signed_obi = float(raw_obi)
        if dir_u == "BUY" and signed_obi <= -min_abs:
            return (
                False,
                f"sovereign_bypass_obi_crash obi={signed_obi:.3f} src={raw_src}",
            )
        if dir_u == "SELL" and signed_obi >= min_abs:
            return (
                False,
                f"sovereign_bypass_obi_meltup obi={signed_obi:.3f} src={raw_src}",
            )
        return (
            True,
            f"sovereign_ml_obi_bypass p={p:.3f} |obi|={obi_mag:.3f} src={obi_src}",
        )
    except Exception as exc:
        return False, f"sovereign_bypass_fail_closed:{type(exc).__name__}"


def _sovereign_ml_obi_bypass(
    epic: str,
    direction: str,
    *,
    bid: float,
    offer: float,
    cfg: Any | None = None,
) -> bool:
    """Backward-compatible bool wrapper for ``sovereign_ml_obi_bypass_qualifies``."""
    ok, _ = sovereign_ml_obi_bypass_qualifies(
        epic, direction, bid=bid, offer=offer, cfg=cfg
    )
    return ok


def _dow_overnight_allowlisted(epic: str, label: str, block: dict[str, Any]) -> bool:
    """DOW night-matrix escape — opt-in only (v33 default off).

    Overnight DOW often sits in MEAN_REVERSION (fade_extremes) with
    strategy_gate.allow_entries=true; allowlist must cover that label or the
    night matrix starves despite operational emerald. NEUTRAL/LOW_VOL kept.
    Still blocked in us_close / rollover chop windows below.
    """
    if str(epic or "") != _DOW_EPIC:
        return False
    if not bool(block.get("dow_overnight_allowlist", False)):
        return False
    if label not in ("NEUTRAL", "LOW_VOL", "MEAN_REVERSION"):
        return False
    # us_close / London chop window — keep blocked even with allowlist
    try:
        now = datetime.now(ZoneInfo("Europe/London"))
        hm = now.hour * 60 + now.minute
        # US cash close / early post-close chop ~20:00–21:30 BST
        if 20 * 60 <= hm < 21 * 60 + 30:
            return False
        # Rollover lock vicinity already handled elsewhere; block 21:55–22:10
        if 21 * 60 + 55 <= hm <= 22 * 60 + 10:
            return False
    except Exception:
        pass
    return True


def evaluate_trending_regime_gate(
    epic: str,
    *,
    cfg: Any | None = None,
    direction: str = "",
    bid: float = 0.0,
    offer: float = 0.0,
) -> tuple[bool, str]:
    """
    Multi-market entries only when regime is strictly trending.

    RANGE_BOUND / NEUTRAL / CHOP / STAGNANT_DZ / MEAN_REVERSION → hard block.
    DOW overnight may pass NEUTRAL / LOW_VOL / MEAN_REVERSION via
    ``dow_overnight_allowlist`` (config opt-in) except during us_close /
    rollover chop windows.
    Missing regime data fails open (warmup) unless fail_closed_missing_regime.
    """
    block: dict[str, Any] = {}
    if cfg is not None and hasattr(cfg, "get"):
        block = dict(cfg.get("pre_entry_regime_veto") or {})
    try:
        from diagnostics.param_tuner import merge_cfg_section

        block = merge_cfg_section(cfg, "pre_entry_regime_veto") or block
    except Exception:
        pass
    if not bool(block.get("require_trending_regime", True)):
        return True, "trending_regime_gate_off"

    label = _resolve_regime_label(epic)
    if not label:
        if bool(block.get("fail_closed_missing_regime", False)):
            return False, "regime_veto_missing_regime"
        return True, "trending_regime_warmup"

    if label in _BLOCKED_REGIMES:
        # ML≥68% + |OBI|≥0.15 bypasses all non-trending labels (RANGE_BOUND,
        # NEUTRAL, MEAN_REVERSION, CHOP, …) — operator-qualified sniper path.
        if direction and bid > 0 and offer > bid:
            bypass_ok, bypass_detail = sovereign_ml_obi_bypass_qualifies(
                epic, direction, bid=bid, offer=offer, cfg=cfg
            )
            if bypass_ok:
                return True, f"{bypass_detail} label={label}"
        if _dow_overnight_allowlisted(epic, label, block):
            return True, f"trending_regime_dow_overnight_allow label={label}"
        return False, f"regime_veto_not_trending label={label}"
    if label in _TRENDING_REGIMES:
        return True, f"trending_regime_ok label={label}"
    return False, f"regime_veto_not_trending label={label}"


@dataclass(frozen=True)
class RegimeDecision:
    allowed: bool
    reason: str
    entry_route: EntryRoute = "MARKET"
    touch_level: float | None = None
    spread: float = 0.0
    spread_ma: float = 0.0
    elasticity_ratio: float = 1.0


_last_decision: RegimeDecision | None = None


def get_last_regime_decision() -> RegimeDecision | None:
    return _last_decision


def evaluate_pre_entry_regime_decision(
    epic: str,
    direction: str,
    *,
    bid: float,
    offer: float,
    cfg: Any | None = None,
) -> RegimeDecision:
    """
    Full structural decision: BLOCK / MARKET / WORKING_ORDER.

    Spread elasticity: if streaming spread > 1.5× 1h MA, forbid MARKET and
    recommend a resting working order at the historical inside touch.
    """
    global _last_decision
    try:
        # Grok Oracle — instant string gate (disk-hot-reloadable, no HTTP)
        from execution.grok_macro_bias import grok_macro_blocks_entries

        blocked, grok_reason = grok_macro_blocks_entries(cfg)
        if blocked:
            d = RegimeDecision(
                allowed=False,
                reason=f"regime_veto_{grok_reason}",
                entry_route="BLOCK",
            )
            _last_decision = d
            return d

        b = float(bid or 0)
        o = float(offer or 0)
        if b <= 0 or o <= b:
            d = RegimeDecision(
                allowed=False,
                reason="regime_veto_invalid_book",
                entry_route="BLOCK",
            )
            _last_decision = d
            return d

        mid = (b + o) / 2.0
        spread = o - b
        spread_pct = spread / mid if mid > 0 else 1.0

        block: dict[str, Any] = {}
        if cfg is not None and hasattr(cfg, "get"):
            block = dict(cfg.get("pre_entry_regime_veto") or {})
        # Hot-reload soft params from tuning_overlay (param_tuner) — no restart
        try:
            from diagnostics.param_tuner import merge_cfg_section

            block = merge_cfg_section(cfg, "pre_entry_regime_veto") or block
        except Exception:
            pass
        if not bool(block.get("enabled", True)):
            d = RegimeDecision(allowed=True, reason="regime_veto_off", entry_route="MARKET")
            _last_decision = d
            return d

        ok_tr, reason_tr = evaluate_trending_regime_gate(
            epic, cfg=cfg, direction=direction, bid=b, offer=o
        )
        if not ok_tr:
            d = RegimeDecision(
                allowed=False,
                reason=reason_tr,
                entry_route="BLOCK",
            )
            _last_decision = d
            return d

        # ── Spread elasticity baseline (in-memory, no I/O) ────────────
        from execution.spread_elasticity import (
            elasticity_cfg,
            historical_inside_touch,
            observe_spread,
            spread_elasticity_state,
        )

        observe_spread(epic, b, o)
        eblock = elasticity_cfg(cfg)
        elasticity_on = bool(eblock.get("enabled", True))
        elast_mult = float(eblock.get("elasticity_mult") or 1.5)
        estate = spread_elasticity_state(
            epic, b, o, elasticity_mult=elast_mult
        )

        from system.memory_context import get_runtime_context, resolve_asset_profile

        profile = resolve_asset_profile(epic)
        rt = get_runtime_context()
        # Primary: RuntimeContext cap (never hardcoded 3.0).
        # FX: price spread × point_multiplier → pips; indices: absolute pts.
        # if current_tick.spread > system_state.active_asset.max_spread_pts: return False
        max_pts = float(profile.max_spread_pts)
        spread_pts = rt.spread_points(epic, spread)
        if max_pts > 0 and spread_pts > max_pts:
            d = RegimeDecision(
                allowed=False,
                reason=f"regime_veto_spread_pts {spread_pts:.2f}>{max_pts:.2f}",
                entry_route="BLOCK",
                spread=spread,
                spread_ma=estate.spread_ma,
                elasticity_ratio=estate.ratio,
            )
            _last_decision = d
            return d

        # Legacy mid-% gate — opt-in only. Global 0.02% false-positives FX/Gold.
        enforce_pct = bool(block.get("enforce_spread_pct", False))
        if enforce_pct and not profile.is_forex:
            max_pct = float(block.get("max_spread_pct") or DEFAULT_MAX_SPREAD_PCT)
            if spread_pct > max_pct:
                d = RegimeDecision(
                    allowed=False,
                    reason=(
                        f"regime_veto_spread_pct {spread_pct*100:.4f}%>{max_pct*100:.4f}%"
                    ),
                    entry_route="BLOCK",
                    spread=spread,
                    spread_ma=estate.spread_ma,
                    elasticity_ratio=estate.ratio,
                )
                _last_decision = d
                return d

        # OBI crash / melt-up
        try:
            from execution.entry_gate_hardening import evaluate_obi_entry_filter
            from types import SimpleNamespace

            quote = SimpleNamespace(bid=b, offer=o, mid=mid)
            ok_o, reason_o, _ = evaluate_obi_entry_filter(
                epic, direction, cfg=cfg, quote=quote
            )
            if not ok_o:
                d = RegimeDecision(
                    allowed=False,
                    reason=f"regime_veto_{reason_o}",
                    entry_route="BLOCK",
                    spread=spread,
                    spread_ma=estate.spread_ma,
                    elasticity_ratio=estate.ratio,
                )
                _last_decision = d
                return d
        except Exception as exc:
            d = RegimeDecision(
                allowed=False,
                reason=f"regime_veto_obi_fail_closed:{type(exc).__name__}",
                entry_route="BLOCK",
            )
            _last_decision = d
            return d

        # Leader-follower futures / proxy micro-momentum
        try:
            from execution.leader_follower_gate import evaluate_leader_follower_gate

            ok_lf, reason_lf = evaluate_leader_follower_gate(
                epic, direction, bid=b, offer=o, cfg=cfg
            )
            if not ok_lf:
                d = RegimeDecision(
                    allowed=False,
                    reason=f"regime_veto_{reason_lf}",
                    entry_route="BLOCK",
                    spread=spread,
                    spread_ma=estate.spread_ma,
                    elasticity_ratio=estate.ratio,
                )
                _last_decision = d
                return d
        except Exception as exc:
            d = RegimeDecision(
                allowed=False,
                reason=f"regime_veto_leader_fail_closed:{type(exc).__name__}",
                entry_route="BLOCK",
            )
            _last_decision = d
            return d

        # Elastic wide vs 1h MA → resting WO at historical inside touch
        if elasticity_on and estate.elastic:
            touch = historical_inside_touch(direction, estate)
            d = RegimeDecision(
                allowed=True,
                reason=(
                    f"regime_route_working_order elasticity={estate.ratio:.2f}x "
                    f"spread={spread:.2f}>1.5*ma={estate.spread_ma:.2f}"
                ),
                entry_route="WORKING_ORDER",
                touch_level=touch,
                spread=spread,
                spread_ma=estate.spread_ma,
                elasticity_ratio=estate.ratio,
            )
            _last_decision = d
            return d

        d = RegimeDecision(
            allowed=True,
            reason="regime_veto_clear",
            entry_route="MARKET",
            spread=spread,
            spread_ma=estate.spread_ma,
            elasticity_ratio=estate.ratio,
        )
        _last_decision = d
        return d
    except Exception as exc:
        d = RegimeDecision(
            allowed=False,
            reason=f"regime_veto_fail_closed:{type(exc).__name__}",
            entry_route="BLOCK",
        )
        _last_decision = d
        return d


def evaluate_pre_entry_regime_veto(
    epic: str,
    direction: str,
    *,
    bid: float,
    offer: float,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """
    Return (allowed, reason) — backward-compatible wrapper.

    WORKING_ORDER routes return allowed=True with a ``regime_route_working_order``
    reason; callers that place MARKET must also inspect
    ``get_last_regime_decision().entry_route``.
    """
    decision = evaluate_pre_entry_regime_decision(
        epic, direction, bid=bid, offer=offer, cfg=cfg
    )
    return decision.allowed, decision.reason
