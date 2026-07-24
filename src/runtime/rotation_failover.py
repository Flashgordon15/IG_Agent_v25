"""Ranked multi-market rotator (SB hot-path allowlist) + legacy DOW-stale failover.

When ``dual_core.rotation_failover_enabled`` is on (default OFF in older configs):

* **Ranked mode** (``ranked_rotator_mode: true`` — preferred): score candidate
  epics each sweep with tradeability/liquidity **and sniper confidence**;
  promote top-N to the effective SB allowlist. DOW is **not** permanently
  privileged — a weaker DOW is demoted when Gold/EURUSD/FTSE rank higher.
* Dominant / prefer flips use hysteresis (``min_confidence_lead``,
  ``min_hold_scans``) so noise does not flip every 2s.
* **Legacy DOW-stale mode**: after DOW stays WAIT / below confidence for
  ``rotation_failover_stale_minutes``, union ``failover_epics`` onto the
  DOW-only allowlist (Gold-only escape hatch).

Does not re-enable Instant / Core-B micro. Does not touch CFD A2 pause.
Nikkei (and other ``exclude_from_hot_path``) never promote.
"""

from __future__ import annotations

import csv
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DOW = "IX.D.DOW.IFM.IP"
GOLD = "CS.D.CFPGOLD.CFP.IP"
EURUSD = "CS.D.EURUSD.CFD.IP"
EURUSD_SB_TODAY = "CS.D.EURUSD.TODAY.IP"
FTSE = "IX.D.FTSE.IFM.IP"
NIKKEI = "IX.D.NIKKEI.IFM.IP"

_DEFAULT_RANKED_CANDIDATES: tuple[str, ...] = (DOW, GOLD, EURUSD, FTSE)
_DEFAULT_FAILOVER_EPICS: tuple[str, ...] = (GOLD, EURUSD, FTSE)

_lock = threading.RLock()
_dow_untradeable_since: float | None = None
_dow_recover_since: float | None = None
_active: bool = False
_reason: str = ""
_promoted: tuple[str, ...] = ()
_ranked_rows: list[dict[str, Any]] = []
_dominant: str | None = None
_mode: str = "off"
_last_rank_at: float = 0.0
_prefer_epic: str | None = None
_preference_reason: str = ""
_hold_challenger: str | None = None
_hold_scans: int = 0
_per_epic_confidence: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class RotationFailoverConfig:
    enabled: bool = False
    ranked_mode: bool = True
    stale_minutes: float = 8.0
    recover_minutes: float = 3.0
    confidence_floor: float = 0.68
    failover_epics: tuple[str, ...] = _DEFAULT_FAILOVER_EPICS
    ranked_candidates: tuple[str, ...] = _DEFAULT_RANKED_CANDIDATES
    promote_top_n: int = 2
    rerank_min_sec: float = 5.0
    use_journal_expectancy: bool = True
    # Sniper / ML confidence tilt on rank_score (p_success * weight).
    confidence_weight: float = 40.0
    # Extra score when approved & p >= threshold (SETUP).
    setup_bonus: float = 8.0
    # Hysteresis: challenger must lead incumbent confidence by this margin.
    min_confidence_lead: float = 0.05
    # Consecutive qualifying scans before dominant/prefer flip.
    min_hold_scans: int = 3


def reset_rotation_failover_for_tests() -> None:
    global _dow_untradeable_since, _dow_recover_since, _active, _reason, _promoted
    global _ranked_rows, _dominant, _mode, _last_rank_at
    global _prefer_epic, _preference_reason, _hold_challenger, _hold_scans
    global _per_epic_confidence
    with _lock:
        _dow_untradeable_since = None
        _dow_recover_since = None
        _active = False
        _reason = ""
        _promoted = ()
        _ranked_rows = []
        _dominant = None
        _mode = "off"
        _last_rank_at = 0.0
        _prefer_epic = None
        _preference_reason = ""
        _hold_challenger = None
        _hold_scans = 0
        _per_epic_confidence = {}


def load_rotation_failover_config(cfg: Any | None) -> RotationFailoverConfig:
    dual: dict[str, Any] = {}
    if cfg is not None and hasattr(cfg, "get"):
        raw = cfg.get("dual_core") or {}
        if isinstance(raw, dict):
            dual = raw

    ranked_raw = (
        dual.get("ranked_candidate_epics")
        or dual.get("rotation_ranked_epics")
        or dual.get("failover_epics")
    )
    if isinstance(ranked_raw, (list, tuple)) and ranked_raw:
        ranked = tuple(str(e).strip() for e in ranked_raw if str(e).strip())
    else:
        ranked = _DEFAULT_RANKED_CANDIDATES
    # Ensure DOW stays in the candidate pool for fair ranking even if config
    # only listed failover alts (legacy Gold-only lists).
    if DOW not in ranked:
        ranked = (DOW,) + ranked

    fail_raw = dual.get("failover_epics") or dual.get("rotation_failover_epics")
    if isinstance(fail_raw, (list, tuple)) and fail_raw:
        failover = tuple(str(e).strip() for e in fail_raw if str(e).strip())
    else:
        failover = tuple(e for e in ranked if e != DOW) or _DEFAULT_FAILOVER_EPICS

    try:
        stale = float(dual.get("rotation_failover_stale_minutes", 8.0) or 8.0)
    except (TypeError, ValueError):
        stale = 8.0
    try:
        recover = float(dual.get("rotation_failover_recover_minutes", 3.0) or 3.0)
    except (TypeError, ValueError):
        recover = 3.0
    try:
        floor = float(dual.get("rotation_failover_confidence_floor", 0.68) or 0.68)
    except (TypeError, ValueError):
        floor = 0.68
    try:
        top_n = int(dual.get("ranked_promote_top_n", dual.get("active_stack_slots", 2)) or 2)
    except (TypeError, ValueError):
        top_n = 2
    try:
        rerank = float(dual.get("ranked_rerank_min_sec", 5.0) or 5.0)
    except (TypeError, ValueError):
        rerank = 5.0
    try:
        conf_w = float(
            dual.get("ranked_confidence_weight", dual.get("confidence_weight", 40.0))
            or 40.0
        )
    except (TypeError, ValueError):
        conf_w = 40.0
    try:
        setup_b = float(dual.get("ranked_setup_bonus", 8.0) or 8.0)
    except (TypeError, ValueError):
        setup_b = 8.0
    try:
        min_lead = float(
            dual.get("min_confidence_lead", dual.get("ranked_min_confidence_lead", 0.05))
            or 0.05
        )
    except (TypeError, ValueError):
        min_lead = 0.05
    try:
        min_hold = int(
            dual.get("min_hold_scans", dual.get("ranked_min_hold_scans", 3)) or 3
        )
    except (TypeError, ValueError):
        min_hold = 3

    # Prefer ranked mode when enabled unless explicitly set false.
    if "ranked_rotator_mode" in dual:
        ranked_mode = bool(dual.get("ranked_rotator_mode"))
    else:
        ranked_mode = True

    return RotationFailoverConfig(
        enabled=bool(dual.get("rotation_failover_enabled", False)),
        ranked_mode=ranked_mode,
        stale_minutes=max(1.0, stale),
        recover_minutes=max(0.5, recover),
        confidence_floor=max(0.50, min(0.95, floor)),
        failover_epics=failover or _DEFAULT_FAILOVER_EPICS,
        ranked_candidates=ranked or _DEFAULT_RANKED_CANDIDATES,
        promote_top_n=max(1, min(6, top_n)),
        rerank_min_sec=max(1.0, rerank),
        use_journal_expectancy=bool(dual.get("ranked_use_journal_expectancy", True)),
        confidence_weight=max(0.0, min(100.0, conf_w)),
        setup_bonus=max(0.0, min(30.0, setup_b)),
        min_confidence_lead=max(0.0, min(0.50, min_lead)),
        min_hold_scans=max(1, min(30, min_hold)),
    )


def _excluded(cfg: Any | None) -> set[str]:
    if cfg is None or not hasattr(cfg, "get"):
        return set()
    dual = cfg.get("dual_core") or {}
    if not isinstance(dual, dict):
        return set()
    return {str(e).strip() for e in (dual.get("exclude_from_hot_path") or []) if str(e).strip()}


def _dow_is_untradeable(
    *,
    p_success: float | None,
    approved: bool | None,
    threshold: float | None,
    confidence_floor: float,
) -> bool:
    """True when DOW is in WAIT / below usable confidence (not inventing scores)."""
    if p_success is None:
        return True
    try:
        p = float(p_success)
    except (TypeError, ValueError):
        return True
    thr = confidence_floor
    if threshold is not None:
        try:
            thr = max(confidence_floor, float(threshold))
        except (TypeError, ValueError):
            thr = confidence_floor
    if approved is False:
        return True
    return p < thr


def _optional_journal_expectancy(epics: tuple[str, ...]) -> dict[str, float]:
    """Best-effort expectancy (£) from recent daily_journal rows — never blocks."""
    out: dict[str, float] = {e: 0.0 for e in epics}
    try:
        from system.paths import data_dir

        path = Path(data_dir()) / "metrics" / "daily_journal.csv"
        if not path.is_file():
            return out
        # Cap read — cheap tail via deque over last ~400 lines.
        from collections import deque

        tail: deque[str] = deque(maxlen=400)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                tail.append(line)
        if not tail:
            return out
        header_line = None
        # Prefer header from file start if present in first line of full file —
        # fall back to scanning for a header-looking row.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            header_line = fh.readline()
        if not header_line:
            return out
        reader = csv.DictReader([header_line, *tail])
        sums: dict[str, list[float]] = {e: [] for e in epics}
        aliases = {
            EURUSD_SB_TODAY: EURUSD,
            "CS.D.EURUSD.DAILY.IP": EURUSD,
        }
        for row in reader:
            if not isinstance(row, dict):
                continue
            epic = str(row.get("epic") or row.get("instrument") or "").strip()
            epic = aliases.get(epic, epic)
            if epic not in sums:
                continue
            raw = row.get("pnl_gbp") or row.get("realized_pnl_gbp") or row.get("pnl")
            try:
                pnl = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            sums[epic].append(pnl)
        for epic, vals in sums.items():
            if vals:
                out[epic] = sum(vals) / len(vals)
    except Exception:
        pass
    return out


def _rotation_composites(epics: tuple[str, ...]) -> dict[str, float]:
    scores: dict[str, float] = {e: 50.0 for e in epics}
    try:
        from runtime.dual_core_execution import get_rotation_state

        rot = get_rotation_state() or {}
        rows = rot.get("rotation_scores") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            epic = str(row.get("epic") or "").strip()
            if epic in scores:
                try:
                    scores[epic] = float(row.get("composite") or 50.0)
                except (TypeError, ValueError):
                    scores[epic] = 50.0
    except Exception:
        pass
    return scores


def _eligible_set(
    *,
    runtime_cfg: Any | None,
    eligible_epics: set[str] | None,
) -> set[str] | None:
    """Return eligibility filter; None means 'no filter / treat as eligible'."""
    if eligible_epics is not None:
        return set(eligible_epics)
    try:
        from runtime.dual_core_execution import get_rotation_state

        rot = get_rotation_state() or {}
        body = rot.get("rotation") if isinstance(rot.get("rotation"), dict) else rot
        rows: list[Any] = []
        if isinstance(body, dict):
            rows.extend(body.get("eligible_instruments") or [])
            rows.extend(body.get("active_instruments") or [])
        got = {
            str(r.get("epic") or "").strip()
            for r in rows
            if isinstance(r, dict) and r.get("epic")
        }
        return got or None
    except Exception:
        return None


def _normalize_confidence_row(
    epic: str,
    raw: Any,
    *,
    default_threshold: float,
) -> dict[str, Any] | None:
    """Normalize override or sniper snapshot into a confidence row."""
    if raw is None:
        return None
    p: float | None = None
    approved: bool | None = None
    thr = default_threshold
    if isinstance(raw, (int, float)):
        p = float(raw)
    elif isinstance(raw, dict):
        if raw.get("p_success") is None and raw.get("confidence") is None:
            return None
        try:
            p = float(raw.get("p_success", raw.get("confidence")))
        except (TypeError, ValueError):
            return None
        if raw.get("approved") is not None:
            approved = bool(raw.get("approved"))
        if raw.get("threshold") is not None:
            try:
                thr = float(raw.get("threshold"))
            except (TypeError, ValueError):
                thr = default_threshold
        # Reject global-cache bleed: snapshot epic must match when present.
        snap_epic = str(raw.get("epic") or "").strip()
        if snap_epic and snap_epic != epic and snap_epic != EURUSD_SB_TODAY:
            if not (epic == EURUSD and snap_epic == EURUSD_SB_TODAY):
                return None
    else:
        return None
    if p is None:
        return None
    p = max(0.0, min(1.0, float(p)))
    if approved is None:
        approved = p >= thr
    mode = "SETUP" if approved and p >= thr else "WAIT"
    return {
        "epic": epic,
        "p_success": round(p, 6),
        "confidence": round(p, 6),
        "approved": bool(approved),
        "threshold": round(float(thr), 4),
        "mode": mode,
    }


def _sniper_confidence_map(
    epics: tuple[str, ...],
    *,
    confidence_floor: float,
) -> dict[str, dict[str, Any]]:
    """Best-effort per-epic sniper cache — never invents scores from global DOW."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from alpha.micro_sniper_ml import latest_sniper_ml_snapshot
    except Exception:
        return out
    for epic in epics:
        try:
            snap = latest_sniper_ml_snapshot(epic=epic)
        except Exception:
            continue
        if not isinstance(snap, dict):
            continue
        # latest_sniper_ml_snapshot falls back to global when epic missing —
        # only accept when the stamped epic matches.
        snap_epic = str(snap.get("epic") or "").strip()
        if snap_epic and snap_epic != epic:
            if not (epic == EURUSD and snap_epic == EURUSD_SB_TODAY):
                continue
        if snap.get("p_success") is None:
            continue
        row = _normalize_confidence_row(
            epic, snap, default_threshold=confidence_floor
        )
        if row:
            out[epic] = row
    return out


def rank_candidate_markets(
    cfg: Any | None = None,
    *,
    eligible_epics: set[str] | None = None,
    score_overrides: dict[str, float] | None = None,
    expectancy_overrides: dict[str, float] | None = None,
    confidence_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Score + rank candidate epics (highest first). Nikkei/excludes never appear.

    Rank score blends rotation composite (tradeability/liquidity), eligibility,
    journal expectancy, and sniper ``p_success`` confidence.
    """
    conf = load_rotation_failover_config(cfg)
    excluded = _excluded(cfg)
    eligible = _eligible_set(runtime_cfg=cfg, eligible_epics=eligible_epics)
    composites = score_overrides if score_overrides is not None else _rotation_composites(
        conf.ranked_candidates
    )
    expectancy = (
        expectancy_overrides
        if expectancy_overrides is not None
        else (
            _optional_journal_expectancy(conf.ranked_candidates)
            if conf.use_journal_expectancy
            else {e: 0.0 for e in conf.ranked_candidates}
        )
    )
    if confidence_overrides is not None:
        conf_map: dict[str, dict[str, Any]] = {}
        for epic, raw in confidence_overrides.items():
            row = _normalize_confidence_row(
                str(epic), raw, default_threshold=conf.confidence_floor
            )
            if row:
                conf_map[str(epic)] = row
    else:
        conf_map = _sniper_confidence_map(
            conf.ranked_candidates, confidence_floor=conf.confidence_floor
        )

    rows: list[dict[str, Any]] = []
    for epic in conf.ranked_candidates:
        if epic in excluded or epic == NIKKEI:
            continue
        # TODAY wire is an alias of EURUSD logical — skip duplicate if both listed.
        if epic == EURUSD_SB_TODAY and EURUSD in conf.ranked_candidates:
            continue
        tradeable = True
        if eligible is not None and epic not in eligible:
            # Still rank, but mark ineligible with a soft penalty so history/
            # config candidates can promote when eligibility snapshot is empty.
            tradeable = False
        composite = float(composites.get(epic, 50.0))
        exp = float(expectancy.get(epic, 0.0))
        # Expectancy is a small tilt (±15 pts), never the sole driver.
        exp_tilt = max(-15.0, min(15.0, exp * 2.0))
        eligible_bonus = 12.0 if tradeable or eligible is None else 0.0
        crow = conf_map.get(epic)
        p_success = float(crow["p_success"]) if crow else None
        approved = bool(crow["approved"]) if crow else None
        thr = float(crow["threshold"]) if crow else conf.confidence_floor
        mode = str(crow["mode"]) if crow else None
        conf_tilt = 0.0
        setup_extra = 0.0
        if p_success is not None:
            conf_tilt = round(float(p_success) * conf.confidence_weight, 2)
            if approved and p_success >= thr:
                setup_extra = conf.setup_bonus
        score = round(
            composite + eligible_bonus + exp_tilt + conf_tilt + setup_extra, 2
        )
        rows.append(
            {
                "epic": epic,
                "score": score,
                "composite": round(composite, 2),
                "eligible": bool(tradeable or eligible is None),
                "expectancy_gbp": round(exp, 3),
                "exp_tilt": round(exp_tilt, 2),
                "confidence": p_success,
                "p_success": p_success,
                "approved": approved,
                "threshold": round(thr, 4),
                "mode": mode,
                "conf_tilt": conf_tilt,
                "setup_bonus": setup_extra,
            }
        )
    rows.sort(key=lambda r: float(r["score"]), reverse=True)
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


def _row_confidence(row: dict[str, Any] | None) -> float:
    if not row:
        return 0.0
    raw = row.get("confidence", row.get("p_success"))
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _short_epic(epic: str | None) -> str:
    if not epic:
        return "?"
    known = {
        DOW: "DOW",
        GOLD: "GOLD",
        EURUSD: "EURUSD",
        EURUSD_SB_TODAY: "EURUSD",
        FTSE: "FTSE",
        NIKKEI: "NIKKEI",
    }
    key = str(epic).strip()
    if key in known:
        return known[key]
    parts = key.split(".")
    for p in parts:
        u = p.upper()
        if u in ("DOW", "NIKKEI", "GOLD", "FTSE", "DAX", "CRUDE", "EURUSD"):
            return u
        if "GOLD" in u:
            return "GOLD"
        if "EURUSD" in u or u.startswith("EUR"):
            return "EURUSD"
    return key[-12:]


def _apply_hysteresis_promotion(
    conf: RotationFailoverConfig,
    rows: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    """Promote top-N with confidence-lead + hold-scan hysteresis on dominant."""
    global _active, _reason, _promoted, _ranked_rows, _dominant, _mode
    global _prefer_epic, _preference_reason, _hold_challenger, _hold_scans
    global _per_epic_confidence

    _ranked_rows = list(rows)
    _per_epic_confidence = {
        str(r["epic"]): {
            "p_success": r.get("p_success"),
            "confidence": r.get("confidence"),
            "approved": r.get("approved"),
            "threshold": r.get("threshold"),
            "mode": r.get("mode"),
            "score": r.get("score"),
            "rank": r.get("rank"),
            "eligible": r.get("eligible"),
        }
        for r in rows
        if r.get("epic")
    }
    _mode = "ranked"

    proposed = str(rows[0]["epic"]) if rows else None
    proposed_conf = _row_confidence(rows[0] if rows else None)
    incumbent = _dominant
    incumbent_row = next((r for r in rows if r.get("epic") == incumbent), None)
    incumbent_conf = _row_confidence(incumbent_row)

    accept = False
    pref_reason = reason

    if not proposed:
        _hold_challenger = None
        _hold_scans = 0
        _active = False
        _promoted = ()
        _dominant = None
        _prefer_epic = None
        _preference_reason = "ranked_no_candidates"
        _reason = _preference_reason
        return get_rotation_failover_state()

    if incumbent is None or incumbent_row is None:
        # Bootstrap or incumbent dropped from ranked set — accept immediately.
        accept = True
        pref_reason = (
            f"prefer {_short_epic(proposed)} "
            f"conf={proposed_conf:.0%} bootstrap"
        )
        _hold_challenger = None
        _hold_scans = 0
    elif proposed == incumbent:
        _hold_challenger = None
        _hold_scans = 0
        accept = False  # keep current; refresh promoted from rows
        pref_reason = (
            f"prefer {_short_epic(incumbent)} "
            f"conf={incumbent_conf:.0%} hold"
        )
        # Still refresh promoted set from current ranking while dominant holds.
        promoted = tuple(
            str(r["epic"]) for r in rows[: conf.promote_top_n] if r.get("epic")
        )
        # Ensure dominant stays first in promoted.
        if incumbent in promoted:
            promoted = (incumbent,) + tuple(e for e in promoted if e != incumbent)
        else:
            promoted = (incumbent,) + promoted[: max(0, conf.promote_top_n - 1)]
        _promoted = promoted[: conf.promote_top_n]
        _dominant = incumbent
        _prefer_epic = incumbent
        _active = True
        _preference_reason = pref_reason
        _reason = reason if reason else pref_reason
        return get_rotation_failover_state()
    else:
        lead = proposed_conf - incumbent_conf
        if lead >= conf.min_confidence_lead:
            if _hold_challenger == proposed:
                _hold_scans += 1
            else:
                _hold_challenger = proposed
                _hold_scans = 1
            if _hold_scans >= conf.min_hold_scans:
                accept = True
                pref_reason = (
                    f"prefer {_short_epic(proposed)} "
                    f"{proposed_conf:.0%} over {_short_epic(incumbent)} "
                    f"{incumbent_conf:.0%} "
                    f"(lead {lead:.0%} ≥ {conf.min_confidence_lead:.0%}, "
                    f"hold {_hold_scans}/{conf.min_hold_scans})"
                )
                _hold_challenger = None
                _hold_scans = 0
            else:
                accept = False
                pref_reason = (
                    f"holding {_short_epic(incumbent)} "
                    f"{incumbent_conf:.0%}; challenger {_short_epic(proposed)} "
                    f"{proposed_conf:.0%} "
                    f"({_hold_scans}/{conf.min_hold_scans})"
                )
        else:
            _hold_challenger = None
            _hold_scans = 0
            accept = False
            pref_reason = (
                f"holding {_short_epic(incumbent)} "
                f"{incumbent_conf:.0%}; {_short_epic(proposed)} "
                f"lead {lead:.0%} < {conf.min_confidence_lead:.0%}"
            )

    if accept:
        promoted = tuple(
            str(r["epic"]) for r in rows[: conf.promote_top_n] if r.get("epic")
        )
        _promoted = promoted
        _dominant = proposed
        _prefer_epic = proposed
        _active = bool(promoted)
        _preference_reason = pref_reason
        _reason = reason if reason else pref_reason
    else:
        # Hold incumbent allowlist; keep prefer pointing at sticky dominant.
        promoted = list(_promoted) if _promoted else []
        if incumbent and incumbent not in promoted:
            promoted = [incumbent] + [e for e in promoted if e != incumbent]
        # Fill remaining slots from current rows without dropping incumbent.
        for r in rows:
            epic = str(r.get("epic") or "")
            if not epic or epic in promoted:
                continue
            if len(promoted) >= conf.promote_top_n:
                break
            promoted.append(epic)
        _promoted = tuple(promoted[: conf.promote_top_n])
        _dominant = incumbent
        _prefer_epic = incumbent
        _active = bool(_promoted)
        _preference_reason = pref_reason
        _reason = reason if reason else pref_reason

    return get_rotation_failover_state()


def _apply_ranked_promotion(
    conf: RotationFailoverConfig,
    rows: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    return _apply_hysteresis_promotion(conf, rows, reason=reason)


def tick_ranked_rotator(
    *,
    cfg: Any | None = None,
    eligible_epics: set[str] | None = None,
    score_overrides: dict[str, float] | None = None,
    expectancy_overrides: dict[str, float] | None = None,
    confidence_overrides: dict[str, Any] | None = None,
    now: float | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Re-rank candidates and promote top-N onto the SB allowlist."""
    global _last_rank_at, _active, _reason, _promoted, _ranked_rows, _dominant, _mode
    global _prefer_epic, _preference_reason, _hold_challenger, _hold_scans
    global _per_epic_confidence
    conf = load_rotation_failover_config(cfg)
    ts = float(now if now is not None else time.time())
    with _lock:
        if not conf.enabled:
            _active = False
            _reason = "disabled"
            _promoted = ()
            _ranked_rows = []
            _dominant = None
            _mode = "off"
            _prefer_epic = None
            _preference_reason = "disabled"
            _hold_challenger = None
            _hold_scans = 0
            _per_epic_confidence = {}
            return get_rotation_failover_state()
        if not conf.ranked_mode:
            return get_rotation_failover_state()
        if (
            not force
            and _last_rank_at > 0
            and (ts - _last_rank_at) < conf.rerank_min_sec
            and _promoted
        ):
            return get_rotation_failover_state()
        rows = rank_candidate_markets(
            cfg,
            eligible_epics=eligible_epics,
            score_overrides=score_overrides,
            expectancy_overrides=expectancy_overrides,
            confidence_overrides=confidence_overrides,
        )
        _last_rank_at = ts
        top = ", ".join(
            f"{r['epic'].split('.')[2] if '.' in r['epic'] else r['epic']}@{r['score']}"
            for r in rows[: conf.promote_top_n]
        )
        return _apply_ranked_promotion(
            conf,
            rows,
            reason=f"ranked_top{conf.promote_top_n}:{top}" if top else "ranked_empty",
        )


def _eligible_candidates_legacy(
    cfg: RotationFailoverConfig,
    *,
    runtime_cfg: Any | None,
    eligible_epics: set[str] | None,
) -> tuple[str, ...]:
    excluded = _excluded(runtime_cfg)
    out: list[str] = []
    for epic in cfg.failover_epics:
        if epic == DOW:
            continue
        if epic in excluded or epic == NIKKEI:
            continue
        if eligible_epics is not None and epic not in eligible_epics:
            continue
        out.append(epic)
    return tuple(out)


def note_dow_tradeability(
    *,
    p_success: float | None,
    approved: bool | None = None,
    threshold: float | None = None,
    cfg: Any | None = None,
    eligible_epics: set[str] | None = None,
    now: float | None = None,
    score_overrides: dict[str, float] | None = None,
    expectancy_overrides: dict[str, float] | None = None,
    confidence_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observe markets; ranked mode re-ranks, else DOW-stale failover timers."""
    global _dow_untradeable_since, _dow_recover_since, _active, _reason, _promoted
    global _ranked_rows, _dominant, _mode
    global _prefer_epic, _preference_reason, _hold_challenger, _hold_scans
    global _per_epic_confidence
    conf = load_rotation_failover_config(cfg)
    ts = float(now if now is not None else time.time())

    with _lock:
        if not conf.enabled:
            _dow_untradeable_since = None
            _dow_recover_since = None
            _active = False
            _reason = "disabled"
            _promoted = ()
            _ranked_rows = []
            _dominant = None
            _mode = "off"
            _prefer_epic = None
            _preference_reason = "disabled"
            _hold_challenger = None
            _hold_scans = 0
            _per_epic_confidence = {}
            return get_rotation_failover_state()

        if conf.ranked_mode:
            # Ranked path — DOW not privileged; promote top-N with hysteresis.
            return tick_ranked_rotator(
                cfg=cfg,
                eligible_epics=eligible_epics,
                score_overrides=score_overrides,
                expectancy_overrides=expectancy_overrides,
                confidence_overrides=confidence_overrides,
                now=ts,
                force=True,
            )

        # ---- Legacy DOW-stale failover ----
        untradeable = _dow_is_untradeable(
            p_success=p_success,
            approved=approved,
            threshold=threshold,
            confidence_floor=conf.confidence_floor,
        )
        _mode = "dow_stale_failover"
        if untradeable:
            _dow_recover_since = None
            if _dow_untradeable_since is None:
                _dow_untradeable_since = ts
            stale_sec = conf.stale_minutes * 60.0
            if (ts - _dow_untradeable_since) >= stale_sec:
                promoted = _eligible_candidates_legacy(
                    conf, runtime_cfg=cfg, eligible_epics=eligible_epics
                )
                if not promoted and eligible_epics is None:
                    excluded = _excluded(cfg)
                    promoted = tuple(
                        e
                        for e in conf.failover_epics
                        if e != DOW and e not in excluded and e != NIKKEI
                    )
                if promoted:
                    _active = True
                    _promoted = promoted
                    _dominant = promoted[0]
                    _prefer_epic = promoted[0]
                    _preference_reason = (
                        f"dow_stale failover → {_short_epic(promoted[0])}"
                    )
                    _reason = (
                        f"dow_stale_{conf.stale_minutes:.0f}m "
                        f"p={p_success} floor={conf.confidence_floor}"
                    )
                else:
                    _active = False
                    _promoted = ()
                    _dominant = None
                    _prefer_epic = None
                    _preference_reason = "dow_stale_no_eligible_failover"
                    _reason = "dow_stale_no_eligible_failover"
        else:
            _dow_untradeable_since = None
            if _active:
                if _dow_recover_since is None:
                    _dow_recover_since = ts
                recover_sec = conf.recover_minutes * 60.0
                if (ts - _dow_recover_since) >= recover_sec:
                    _active = False
                    _promoted = ()
                    _dominant = None
                    _prefer_epic = DOW
                    _preference_reason = "dow_recovered"
                    _reason = "dow_recovered"
                    _dow_recover_since = None
            else:
                _dow_recover_since = None
                _reason = "dow_tradeable"
                _prefer_epic = DOW
                _preference_reason = "dow_tradeable"

        return get_rotation_failover_state()


def get_rotation_failover_state() -> dict[str, Any]:
    with _lock:
        return {
            "rotation_failover_active": bool(_active),
            "rotation_failover_reason": str(_reason),
            "rotation_failover_promoted": list(_promoted),
            "ranked_rotator_mode": _mode == "ranked",
            "ranked_rotator_mode_label": str(_mode),
            "ranked_rotator_dominant": _dominant,
            "ranked_rotator_rows": list(_ranked_rows),
            "prefer_epic": _prefer_epic,
            "preference_reason": str(_preference_reason),
            "per_epic_confidence": dict(_per_epic_confidence),
            "hold_challenger": _hold_challenger,
            "hold_scans": int(_hold_scans),
            "dow_untradeable_since": _dow_untradeable_since,
            "dow_recover_since": _dow_recover_since,
        }


def failover_allows_epic(epic: str, cfg: Any | None = None) -> bool:
    """True when epic is currently promoted onto the SB hot-path allowlist."""
    key = str(epic or "").strip()
    if not key:
        return False
    # TODAY wire counts as EURUSD logical for allow checks.
    logical = EURUSD if key == EURUSD_SB_TODAY else key
    conf = load_rotation_failover_config(cfg)
    if not conf.enabled:
        return False
    if logical in _excluded(cfg) or logical == NIKKEI:
        return False
    with _lock:
        promoted = set(_promoted)
        if EURUSD in promoted:
            promoted.add(EURUSD_SB_TODAY)
        return bool(_active and (key in promoted or logical in promoted))


def effective_sb_allowlist(
    base: set[str] | None,
    cfg: Any | None = None,
) -> set[str] | None:
    """Effective SB allowlist under ranked / legacy failover.

    Ranked mode: returns the top-N promoted set (replaces static DOW-only base
    so a weak DOW can be demoted). Legacy: unions promoted onto base when active.
    """
    conf = load_rotation_failover_config(cfg)
    if base is None:
        return None
    if not conf.enabled:
        return set(base)
    with _lock:
        if not _active or not _promoted:
            return set(base)
        if conf.ranked_mode and _mode == "ranked":
            out = set(_promoted)
            if EURUSD in out:
                out.add(EURUSD_SB_TODAY)
            return out
        return set(base) | set(_promoted)


def tick_rotation_failover_from_sniper(*, cfg: Any | None = None) -> dict[str, Any]:
    """Pull latest snapshots and update ranked / failover state (best-effort)."""
    conf = load_rotation_failover_config(cfg)
    if not conf.enabled:
        return note_dow_tradeability(p_success=None, approved=False, cfg=cfg)

    p_success = None
    approved = None
    threshold = None
    confidence_overrides: dict[str, Any] = {}
    try:
        from alpha.micro_sniper_ml import latest_sniper_ml_snapshot

        snap = latest_sniper_ml_snapshot(epic=DOW)
        if str(snap.get("epic") or "") in ("", DOW):
            p_success = snap.get("p_success")
            approved = snap.get("approved")
            threshold = snap.get("threshold")
        confidence_overrides = _sniper_confidence_map(
            conf.ranked_candidates, confidence_floor=conf.confidence_floor
        )
    except Exception:
        pass

    eligible: set[str] | None = None
    try:
        from runtime.dual_core_execution import get_rotation_state

        rot = get_rotation_state() or {}
        body = rot.get("rotation") if isinstance(rot.get("rotation"), dict) else rot
        rows = []
        if isinstance(body, dict):
            rows.extend(body.get("eligible_instruments") or [])
            rows.extend(body.get("active_instruments") or [])
        eligible = {
            str(r.get("epic") or "").strip()
            for r in rows
            if isinstance(r, dict) and r.get("epic")
        } or None
    except Exception:
        eligible = None

    return note_dow_tradeability(
        p_success=p_success,
        approved=approved,
        threshold=threshold,
        cfg=cfg,
        eligible_epics=eligible,
        confidence_overrides=confidence_overrides or None,
    )
