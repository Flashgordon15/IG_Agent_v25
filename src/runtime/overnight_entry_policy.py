"""Overnight entry lockdown + ML pre-submit attribution + selectivity gates.

Feature flags default to CURRENT (soak) behaviour — no live flip until
``overnight_entry_lockdown.enabled`` / ``ml_unblind.enabled`` / selectivity
thresholds are set in config after Nightmare Night + Profit Barrage suites
are green.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")

ACCT_CFD = "Z6BAH4"
ACCT_SB = "Z6BAH3"
DOW = "IX.D.DOW.IFM.IP"

PATH_INSTANT = "instant"
PATH_MICRO = "micro"
PATH_LONG_RUNNER = "long_trade_runner"
PATH_CORE_B = "core_b"
PATH_OTHER = "other"

_SCALP_PATHS = frozenset({PATH_INSTANT, PATH_MICRO})


def _cfg_block(cfg: Any | None, key: str) -> dict[str, Any]:
    if cfg is None or not hasattr(cfg, "get"):
        return {}
    block = cfg.get(key) or {}
    return dict(block) if isinstance(block, dict) else {}


def overnight_lockdown_enabled(cfg: Any | None = None) -> bool:
    """False by default — CURRENT soak allows overnight Instant/micro."""
    return bool(_cfg_block(cfg, "overnight_entry_lockdown").get("enabled", False))


def ml_unblind_enabled(cfg: Any | None = None) -> bool:
    """False by default — CURRENT does not hard-abort on null ML stamp."""
    return bool(_cfg_block(cfg, "ml_unblind").get("enabled", False))


def in_overnight_lockdown_window(
    now: datetime | None = None,
    *,
    cfg: Any | None = None,
) -> bool:
    """21:00–07:00 Europe/London (wraps midnight)."""
    block = _cfg_block(cfg, "overnight_entry_lockdown")
    start_s = str(block.get("start") or "21:00")
    end_s = str(block.get("end") or "07:00")
    try:
        sh, sm = (int(x) for x in start_s.split(":")[:2])
        eh, em = (int(x) for x in end_s.split(":")[:2])
    except (TypeError, ValueError):
        sh, sm, eh, em = 21, 0, 7, 0
    if now is None:
        now = datetime.now(tz=_LONDON)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_LONDON)
    else:
        now = now.astimezone(_LONDON)
    t = now.timetz().replace(tzinfo=None)
    start = dtime(sh, sm)
    end = dtime(eh, em)
    if start <= end:
        return start <= t < end
    # Wraps midnight (21:00 → 07:00).
    return t >= start or t < end


def resolve_account_id(cfg: Any | None = None, account_id: str | None = None) -> str:
    if account_id:
        return str(account_id).strip()
    try:
        from system.engine_lane import resolve_journal_metadata

        meta = resolve_journal_metadata(cfg=cfg)
        return str(meta.get("account_id") or "")
    except Exception:
        pass
    try:
        import os

        return str(os.environ.get("IG_ACCOUNT_ID") or "").strip()
    except Exception:
        return ""


def classify_entry_path(path: str | None) -> str:
    p = str(path or PATH_OTHER).strip().lower()
    if p in ("instant", "instant_scalp", "micro_scalp_instant"):
        return PATH_INSTANT
    if p in ("micro", "micro_scalp", "core_b_micro"):
        return PATH_MICRO
    if p in ("long_trade_runner", "long_runner", "ltr", "macro_long"):
        return PATH_LONG_RUNNER
    if p in ("core_b", "coreb", "signal_engine"):
        return PATH_CORE_B
    return PATH_OTHER


@dataclass(frozen=True)
class OvernightEntryDecision:
    allow: bool
    reason: str
    in_window: bool
    path: str
    account_id: str


def evaluate_overnight_entry_policy(
    *,
    epic: str,
    path: str,
    account_id: str | None = None,
    cfg: Any | None = None,
    now: datetime | None = None,
    long_runner_gates_ok: bool = False,
) -> OvernightEntryDecision:
    """Gate NEW entries during the London overnight window.

    Management / exits are out of scope (callers only invoke on entry).

    When lockdown disabled → always allow (CURRENT soak behaviour).
    When enabled + in window:
      - CFD (Z6BAH4): ALL new entries blocked
      - SB (Z6BAH3): Instant/micro rejected; long_trade_runner only when
        ``long_runner_gates_ok`` (P/OBI/trend); non-DOW hot-path rejected
    """
    acct = resolve_account_id(cfg, account_id)
    path_k = classify_entry_path(path)
    in_win = in_overnight_lockdown_window(now, cfg=cfg)
    if not overnight_lockdown_enabled(cfg):
        return OvernightEntryDecision(
            allow=True,
            reason="overnight_lockdown_disabled",
            in_window=in_win,
            path=path_k,
            account_id=acct,
        )
    if not in_win:
        return OvernightEntryDecision(
            allow=True,
            reason="outside_overnight_window",
            in_window=False,
            path=path_k,
            account_id=acct,
        )

    epic_s = str(epic or "").strip()
    is_cfd = acct == ACCT_CFD or (
        not acct and str((_cfg_block(cfg, "dual_core").get("broker_account_product") or "")).upper()
        == "CFD"
    )
    # Prefer explicit account; CFD engine origin also maps to CFD lane.
    if not acct:
        try:
            import os

            origin = str(os.environ.get("IG_ENGINE_ORIGIN") or "").upper()
            if origin == "QUANT_SNIPER":
                is_cfd = True
                acct = ACCT_CFD
            elif origin == "MACRO_SENTINEL":
                is_cfd = False
                acct = ACCT_SB
        except Exception:
            pass

    if is_cfd or acct == ACCT_CFD:
        return OvernightEntryDecision(
            allow=False,
            reason="overnight_cfd_new_entries_blocked",
            in_window=True,
            path=path_k,
            account_id=acct or ACCT_CFD,
        )

    # SB lane
    if epic_s and epic_s != DOW:
        return OvernightEntryDecision(
            allow=False,
            reason="overnight_sb_non_dow_rejected",
            in_window=True,
            path=path_k,
            account_id=acct or ACCT_SB,
        )

    if path_k in _SCALP_PATHS:
        return OvernightEntryDecision(
            allow=False,
            reason="overnight_sb_instant_micro_rejected",
            in_window=True,
            path=path_k,
            account_id=acct or ACCT_SB,
        )

    if path_k == PATH_LONG_RUNNER:
        if long_runner_gates_ok:
            return OvernightEntryDecision(
                allow=True,
                reason="overnight_sb_long_runner_ok",
                in_window=True,
                path=path_k,
                account_id=acct or ACCT_SB,
            )
        return OvernightEntryDecision(
            allow=False,
            reason="overnight_sb_long_runner_gates_fail",
            in_window=True,
            path=path_k,
            account_id=acct or ACCT_SB,
        )

    # Core B / other overnight on SB — treat as blocked (only LTR allowed).
    return OvernightEntryDecision(
        allow=False,
        reason="overnight_sb_path_not_long_runner",
        in_window=True,
        path=path_k,
        account_id=acct or ACCT_SB,
    )


def is_finite_ml_probability(value: Any) -> bool:
    try:
        if value is None:
            return False
        p = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(p)


def normalize_ml_probability(value: Any) -> float | None:
    """Canonical 0..1 scale. Accepts 0-1 or 0-100 percent."""
    if not is_finite_ml_probability(value):
        return None
    p = float(value)
    if p > 1.0 and p <= 100.0:
        p = p / 100.0
    if p < 0.0 or p > 1.0:
        return None
    return p


@dataclass(frozen=True)
class MlPreSubmitResult:
    allow_submit: bool
    p_success: float | None
    reason: str
    ml_score_at_entry: float | None
    scorer: str


def score_entry_candidate_ml(
    *,
    epic: str,
    direction: str,
    cfg: Any | None = None,
    quote: Any | None = None,
    p_success: float | None = None,
    scorer_name: str = "sniper_ml",
    invoke_scorer: bool = True,
) -> MlPreSubmitResult:
    """Pre-submit ML attribution for entry-candidates (not every quote tick).

    When ``ml_unblind.enabled``:
      - null/NaN/missing → hard-abort (no submit)
      - finite p stamped as ml_score_at_entry on accept
    When disabled (CURRENT): missing score does not abort; returns soft allow.
    """
    p = normalize_ml_probability(p_success)
    scorer = str(scorer_name or "sniper_ml")
    if p is None and invoke_scorer:
        try:
            from execution.entry_gate_hardening import evaluate_sniper_ml_gate

            _ok, _detail, raw_p = evaluate_sniper_ml_gate(
                str(epic or ""),
                str(direction or "BUY"),
                cfg=cfg,
                quote=quote,
            )
            p = normalize_ml_probability(raw_p)
            scorer = "sniper_ml"
        except Exception as exc:
            if ml_unblind_enabled(cfg):
                return MlPreSubmitResult(
                    allow_submit=False,
                    p_success=None,
                    reason=f"ml_pre_submit_fail_closed:{type(exc).__name__}",
                    ml_score_at_entry=None,
                    scorer=scorer,
                )
            return MlPreSubmitResult(
                allow_submit=True,
                p_success=None,
                reason=f"ml_pre_submit_soft:{type(exc).__name__}",
                ml_score_at_entry=None,
                scorer=scorer,
            )

    if p is None:
        if ml_unblind_enabled(cfg):
            return MlPreSubmitResult(
                allow_submit=False,
                p_success=None,
                reason="ml_score_null_abort",
                ml_score_at_entry=None,
                scorer=scorer,
            )
        return MlPreSubmitResult(
            allow_submit=True,
            p_success=None,
            reason="ml_score_missing_current",
            ml_score_at_entry=None,
            scorer=scorer,
        )

    return MlPreSubmitResult(
        allow_submit=True,
        p_success=p,
        reason="ml_score_ok",
        ml_score_at_entry=p,
        scorer=scorer,
    )


def selectivity_thresholds(cfg: Any | None = None) -> tuple[float, float, bool]:
    """Return (min_p, min_abs_obi, require_15m_trend_ml_obi)."""
    instant = _cfg_block(cfg, "micro_scalp_instant")
    dual = _cfg_block(cfg, "dual_core")
    sel = _cfg_block(cfg, "selectivity_gates")
    min_p = float(
        sel.get("min_ml_p_success")
        or instant.get("min_ml_p_success")
        or 0.55
    )
    min_obi = float(sel.get("min_abs_obi") or 0.0)
    require = bool(
        sel.get(
            "require_15m_trend_ml_obi",
            instant.get(
                "require_15m_trend_ml_obi",
                dual.get("cfd_require_15m_trend_ml_obi", False),
            ),
        )
    )
    return min_p, min_obi, require


@dataclass(frozen=True)
class SelectivityDecision:
    allow: bool
    reason: str
    p_success: float | None
    abs_obi: float
    trend_ok: bool


def evaluate_selectivity_gates(
    *,
    epic: str,
    direction: str,
    p_success: float | None,
    obi: float | None,
    trend_15m: str | None = None,
    cfg: Any | None = None,
    force_require: bool | None = None,
) -> SelectivityDecision:
    """P / |OBI| / 15m trend agree gates (DOW-centric selectivity)."""
    min_p, min_obi, require = selectivity_thresholds(cfg)
    if force_require is not None:
        require = bool(force_require)
    p = normalize_ml_probability(p_success)
    try:
        abs_obi = abs(float(obi if obi is not None else 0.0))
    except (TypeError, ValueError):
        abs_obi = 0.0
    dir_u = str(direction or "").upper()
    trend_u = str(trend_15m or "").upper()
    trend_ok = (
        (trend_u == "BULLISH" and dir_u == "BUY")
        or (trend_u == "BEARISH" and dir_u == "SELL")
        or (not require)
    )
    if not require:
        return SelectivityDecision(
            allow=True,
            reason="selectivity_not_required",
            p_success=p,
            abs_obi=abs_obi,
            trend_ok=True,
        )
    if p is None or p < min_p:
        return SelectivityDecision(
            allow=False,
            reason=f"selectivity_p_fail p={p}<{min_p}",
            p_success=p,
            abs_obi=abs_obi,
            trend_ok=trend_ok,
        )
    if abs_obi < min_obi:
        return SelectivityDecision(
            allow=False,
            reason=f"selectivity_obi_fail |obi|={abs_obi:.3f}<{min_obi}",
            p_success=p,
            abs_obi=abs_obi,
            trend_ok=trend_ok,
        )
    if not trend_ok:
        return SelectivityDecision(
            allow=False,
            reason=f"selectivity_trend_disagree trend={trend_u}",
            p_success=p,
            abs_obi=abs_obi,
            trend_ok=False,
        )
    epic_s = str(epic or "").strip()
    if epic_s and epic_s != DOW:
        # Hot-path SB / Instant selectivity is DOW-centric.
        allow_non_dow = bool(_cfg_block(cfg, "selectivity_gates").get("allow_non_dow", False))
        if not allow_non_dow:
            return SelectivityDecision(
                allow=False,
                reason="selectivity_non_dow_rejected",
                p_success=p,
                abs_obi=abs_obi,
                trend_ok=True,
            )
    return SelectivityDecision(
        allow=True,
        reason="selectivity_ok",
        p_success=p,
        abs_obi=abs_obi,
        trend_ok=True,
    )


def long_runner_overnight_gates_ok(
    *,
    p_success: float | None,
    obi: float | None,
    trend_15m: str | None,
    direction: str,
    cfg: Any | None = None,
) -> bool:
    """P/OBI/trend criteria for SB overnight long_trade_runner path."""
    # Force require for overnight LTR even if Instant soak flag is off.
    decision = evaluate_selectivity_gates(
        epic=DOW,
        direction=direction,
        p_success=p_success,
        obi=obi,
        trend_15m=trend_15m,
        cfg=cfg,
        force_require=True,
    )
    return bool(decision.allow)
