"""Shadow loss loop — APP/LOGIC split + LOGIC-only ML counterfactual.

Operating model (docs/LEARNING_LOOP_PLAN.md):

  Recent losers
    → lifecycle loss autopsy (APP / LOGIC / UNKNOWN)
    → APP tickets only (do NOT feed into ML edge claims)
    → LOGIC losers only → shadow re-score (sniper + long paths)
    → report: would ML/gates have vetoed? counterfactual
    → NEVER mix APP+LOGIC in one ML train as "edge"

UNKNOWN is treated as APP until stamped (excluded from ML shadow score set).

Read-only: never places orders, never lifts A2, never claims improvement epoch.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from diagnostics.trade_lifecycle_witness import (
    build_loss_autopsy,
    default_data_root,
)

LOSS_CLASSES = ("APP", "LOGIC", "UNKNOWN")
DEFAULT_MIN_ML_PROBABILITY = 0.52
DEFAULT_LOWER_CONF_DELTA = 0.05


@dataclass
class ClassBucket:
    label: str
    count: int = 0
    pnl_gbp: float = 0.0
    deals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "count": self.count,
            "pnl_gbp": round(self.pnl_gbp, 2),
            "deal_ids": [d.get("deal_id") for d in self.deals],
        }


def normalize_loss_class(raw: Any) -> str:
    val = str(raw or "").strip().upper()
    if val in LOSS_CLASSES:
        return val
    return "UNKNOWN"


def effective_ml_class(loss_class: Any) -> str:
    """UNKNOWN counts as APP for ML exclusion until stamps land."""
    klass = normalize_loss_class(loss_class)
    if klass == "LOGIC":
        return "LOGIC"
    return "APP"


def is_ml_shadow_eligible(row: Mapping[str, Any]) -> bool:
    """True only for LOGIC losers — APP and UNKNOWN are excluded."""
    return effective_ml_class(row.get("loss_class")) == "LOGIC"


def load_autopsy_losers(
    data_root: Path,
    *,
    day: str,
    rebuild_if_missing: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Load losers from loss_autopsy_*.json; optionally rebuild autopsy."""
    path = data_root / "reports" / f"loss_autopsy_{day}.json"
    report: dict[str, Any] | None = None
    if path.is_file():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            report = None
    losers: list[dict[str, Any]] = []
    if isinstance(report, dict):
        raw = report.get("losers")
        if isinstance(raw, list) and raw:
            losers = [dict(x) for x in raw if isinstance(x, Mapping)]
        elif isinstance(report.get("top_losers"), list):
            # Older autopsy files only stored top_losers.
            losers = [dict(x) for x in report["top_losers"] if isinstance(x, Mapping)]
    if not losers and rebuild_if_missing:
        report = build_loss_autopsy(day=day, data_root=data_root)
        losers = [dict(x) for x in (report.get("losers") or []) if isinstance(x, Mapping)]
        # Persist upgraded autopsy so subsequent runs see the full loser list.
        reports_dir = data_root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        out = reports_dir / f"loss_autopsy_{day}.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return losers, report


def split_losers_by_class(
    losers: Sequence[Mapping[str, Any]],
) -> dict[str, ClassBucket]:
    """Split autopsy losers into APP / LOGIC / UNKNOWN with counts + £."""
    buckets = {k: ClassBucket(label=k) for k in LOSS_CLASSES}
    for row in losers:
        klass = normalize_loss_class(row.get("loss_class"))
        b = buckets[klass]
        b.count += 1
        try:
            pnl = float(row.get("pnl_gbp") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        b.pnl_gbp += pnl
        b.deals.append(dict(row))
    return buckets


def ml_shadow_train_score_set(
    losers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """LOGIC-only set for shadow score / future ML train. APP+UNKNOWN excluded."""
    return [dict(r) for r in losers if is_ml_shadow_eligible(r)]


def excluded_from_ml_set(
    losers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """APP + UNKNOWN (UNKNOWN treated as APP) — tickets only, never ML edge."""
    return [dict(r) for r in losers if not is_ml_shadow_eligible(r)]


def _resolve_path_style(row: Mapping[str, Any]) -> str:
    try:
        from ml.style_epic import resolve_ml_style

        return resolve_ml_style(
            style_hint=row.get("style"),
            engine_origin=row.get("engine_origin"),
            exit_reason=row.get("exit_reason"),
            hold_sec=row.get("hold_sec"),
        )
    except Exception:
        return "unknown"


def _min_ml_probability(cfg: Any | None = None) -> float:
    try:
        if cfg is None:
            from system.config import get_config

            cfg = get_config()
        pol = cfg.get("profit_philosophy") if cfg is not None else None
        if isinstance(pol, dict) and pol.get("min_ml_probability") is not None:
            return float(pol["min_ml_probability"])
    except Exception:
        pass
    return DEFAULT_MIN_ML_PROBABILITY


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def shadow_counterfactual_for_row(
    row: Mapping[str, Any],
    *,
    min_ml_probability: float | None = None,
    lower_conf_delta: float = DEFAULT_LOWER_CONF_DELTA,
    current_model_score: float | None = None,
    model_available: bool = False,
    require_ml_probability: bool | None = None,
) -> dict[str, Any]:
    """Would current ML / profit_policy have vetoed or lowered conviction?

    Primary signal: stamped ``ml_score_at_entry`` vs live ``min_ml_probability``.
    Mirrors ``ml.profit_policy``: absent ML (when required) and classic sniper
    gate default ``0.68`` are fail-closed vetoes — not model edge.
    """
    floor = (
        float(min_ml_probability)
        if min_ml_probability is not None
        else DEFAULT_MIN_ML_PROBABILITY
    )
    if require_ml_probability is None:
        require_ml_probability = _require_ml_probability()
    stamped = _f(row.get("ml_score_at_entry"))
    path_style = _resolve_path_style(row)

    score_used = stamped
    score_source = "stamp"
    if current_model_score is not None:
        score_used = float(current_model_score)
        score_source = "current_model"
    elif stamped is None:
        score_source = "unavailable"

    would_veto = False
    would_lower = False
    reason = "no_ml_score"

    # Fail-closed: gate-default 0.68 is not an inference (2026-07-24 pollution).
    try:
        from diagnostics.stamp_provenance import is_threshold_constant

        if score_used is not None and abs(float(score_used) - 0.68) < 1e-6:
            would_veto = True
            reason = f"ml_prob {float(score_used):.3f} is threshold_constant (not inference)"
            score_source = "threshold_constant"
        elif score_used is not None and is_threshold_constant(float(score_used)):
            # Elastic-band edges stay measurable; only 0.68 hard-vetoes live.
            # Journal hygiene still tags other thr constants as untrusted.
            pass
    except Exception:
        pass

    if not would_veto and require_ml_probability and score_used is None:
        would_veto = True
        reason = "ml_prob absent — require_ml_probability"

    if not would_veto and score_used is not None:
        would_veto = score_used < floor
        if stamped is not None and current_model_score is not None:
            would_lower = (stamped - current_model_score) >= float(lower_conf_delta)
        elif stamped is not None and not would_veto:
            would_lower = (floor <= stamped < floor + float(lower_conf_delta))
        if would_veto:
            reason = f"ml_prob {score_used:.3f} < {floor:.2f}"
        elif would_lower:
            reason = f"near_floor_or_model_down score={score_used:.3f} floor={floor:.2f}"
        else:
            reason = f"would_pass score={score_used:.3f} >= {floor:.2f}"

    return {
        "deal_id": row.get("deal_id"),
        "epic": row.get("epic"),
        "pnl_gbp": _f(row.get("pnl_gbp")),
        "loss_class": normalize_loss_class(row.get("loss_class")),
        "path_style": path_style,
        "engine_origin": row.get("engine_origin"),
        "exit_reason": row.get("exit_reason"),
        "hold_sec": _f(row.get("hold_sec")),
        "ml_score_at_entry": stamped,
        "current_model_score": current_model_score,
        "score_used": score_used,
        "score_source": score_source,
        "min_ml_probability": floor,
        "require_ml_probability": bool(require_ml_probability),
        "would_veto": would_veto,
        "would_lower_confidence": would_lower,
        "model_available": model_available,
        "reason": reason,
    }


def _require_ml_probability() -> bool:
    try:
        from system.config_loader import get_config

        cfg = get_config()
        pol = (cfg.get("profit_philosophy") or {}) if hasattr(cfg, "get") else {}
        return bool(pol.get("require_ml_probability", True))
    except Exception:
        return True


def run_entry_policy_counterfactual(
    losers: Sequence[Mapping[str, Any]],
    *,
    min_ml_probability: float | None = None,
) -> dict[str, Any]:
    """Score ALL losers under current entry policy (not ML-train eligible set).

    Answers: would today's profit_policy / 0.68 reject have blocked this bleed?
    Separates APP structural gaps (A2 / risk-stack) that need supervision code.
    """
    floor = (
        float(min_ml_probability)
        if min_ml_probability is not None
        else _min_ml_probability()
    )
    samples = [
        shadow_counterfactual_for_row(row, min_ml_probability=floor)
        for row in losers
    ]
    veto = [s for s in samples if s.get("would_veto")]
    by_class: Counter[str] = Counter(
        str(s.get("loss_class") or "UNKNOWN") for s in veto
    )
    by_reason: Counter[str] = Counter()
    for s in veto:
        r = str(s.get("reason") or "")
        if "threshold_constant" in r:
            by_reason["threshold_constant_0.68"] += 1
        elif "absent" in r:
            by_reason["ml_absent"] += 1
        elif "<" in r:
            by_reason["below_floor"] += 1
        else:
            by_reason["other"] += 1

    # Structural APP buckets from autopsy policy breach codes (not ML).
    structural: dict[str, dict[str, float | int]] = {}
    for row in losers:
        codes = list(row.get("policy_breach_codes") or [])
        for code in codes:
            code_s = str(code or "").strip()
            if code_s in (
                "RISK_STACK_DID_NOT_CUT",
                "SUPERVISION_GAP",
                "CFD_ENTRY_WHILE_A2_PAUSED",
                "EXCLUDED_EPIC",
                "HOLD_LT_MACRO_INTENT",
            ):
                bucket = structural.setdefault(
                    code_s, {"count": 0, "pnl_gbp": 0.0}
                )
                bucket["count"] = int(bucket["count"]) + 1
                bucket["pnl_gbp"] = round(
                    float(bucket["pnl_gbp"]) + float(row.get("pnl_gbp") or 0.0), 2
                )

    veto_gbp = sum(float(s.get("pnl_gbp") or 0.0) for s in veto)
    return {
        "losers": len(losers),
        "would_veto_count": len(veto),
        "would_veto_rate": round(len(veto) / len(losers), 4) if losers else None,
        "would_veto_pnl_gbp": round(veto_gbp, 2),
        "min_ml_probability": floor,
        "require_ml_probability": _require_ml_probability(),
        "by_loss_class": dict(by_class),
        "by_veto_reason": dict(by_reason),
        "app_structural_targets": structural,
        "note": (
            "Entry-policy counterfactual on ALL losers (incl. APP). "
            "Do not treat APP vetoes as ML edge — they are gate hygiene. "
            "Structural APP codes need supervision/deploy (grace flatten, A2)."
        ),
        "samples_head": veto[:25],
    }


def _try_current_model_score(row: Mapping[str, Any]) -> tuple[float | None, bool]:
    """Best-effort current-model predict from sparse row fields.

    Without entry OHLC we cannot rebuild widened features honestly — return
    (None, trained?) so the report stays stamp-primary.
    """
    try:
        from trading.ml_scorer import get_ml_scorer

        scorer = get_ml_scorer()
        trained = bool(scorer.is_trained())
    except Exception:
        return None, False
    # Sparse journal rows lack OHLC; do not invent features for a fake re-score.
    return None, trained


def run_shadow_on_logic(
    logic_rows: Sequence[Mapping[str, Any]],
    *,
    min_ml_probability: float | None = None,
) -> dict[str, Any]:
    floor = (
        float(min_ml_probability)
        if min_ml_probability is not None
        else _min_ml_probability()
    )
    samples: list[dict[str, Any]] = []
    model_available = False
    for row in logic_rows:
        model_score, trained = _try_current_model_score(row)
        model_available = model_available or trained
        samples.append(
            shadow_counterfactual_for_row(
                row,
                min_ml_probability=floor,
                current_model_score=model_score,
                model_available=trained,
            )
        )

    scored = [s for s in samples if s.get("score_used") is not None]
    veto_n = sum(1 for s in scored if s.get("would_veto"))
    lower_n = sum(1 for s in scored if s.get("would_lower_confidence"))
    by_path = Counter(str(s.get("path_style") or "unknown") for s in samples)
    veto_gbp = sum(
        float(s.get("pnl_gbp") or 0.0) for s in scored if s.get("would_veto")
    )
    return {
        "logic_count": len(logic_rows),
        "scored_count": len(scored),
        "unscored_count": len(logic_rows) - len(scored),
        "would_veto_count": veto_n,
        "would_veto_rate": round(veto_n / len(scored), 4) if scored else None,
        "would_lower_confidence_count": lower_n,
        "would_veto_pnl_gbp": round(veto_gbp, 2),
        "min_ml_probability": floor,
        "model_available": model_available,
        "score_mode": "stamp_vs_profit_policy_floor",
        "by_path_style": dict(by_path),
        "samples": samples,
    }


def recommend_next_one_step(
    buckets: Mapping[str, ClassBucket],
    shadow: Mapping[str, Any],
) -> dict[str, Any]:
    app_n = int(buckets.get("APP", ClassBucket("APP")).count)
    unk_n = int(buckets.get("UNKNOWN", ClassBucket("UNKNOWN")).count)
    logic_n = int(buckets.get("LOGIC", ClassBucket("LOGIC")).count)
    app_effective = app_n + unk_n  # UNKNOWN → APP until stamped
    total = app_n + unk_n + logic_n

    if total == 0:
        return {
            "lane": "NONE",
            "action": "no_losers",
            "detail": "No losers in window — keep supervision armed; no strategy change.",
        }
    if app_effective >= max(logic_n, 1) and app_effective >= (total * 0.4):
        return {
            "lane": "APP",
            "action": "fix_app_then_rerun_autopsy",
            "detail": (
                f"APP+UNKNOWN dominate ({app_effective}/{total}). "
                "Ticket stamp/path fixes only; do not feed into ML edge claims. "
                "Re-run autopsy after deploy."
            ),
        }
    veto_rate = shadow.get("would_veto_rate")
    if logic_n > 0 and veto_rate is not None and float(veto_rate) >= 0.5:
        return {
            "lane": "LOGIC",
            "action": "ml_veto_learning",
            "detail": (
                f"LOGIC set would-veto rate={veto_rate:.0%} under current floor. "
                "One change: tighten ML veto / profit_policy — not bundled with APP work."
            ),
        }
    if logic_n > app_effective:
        return {
            "lane": "LOGIC",
            "action": "one_logic_parameter_change",
            "detail": (
                f"LOGIC dominates with clean(er) stamps ({logic_n}/{total}). "
                "One parameter/rule change + witness window — not ML train on APP rows."
            ),
        }
    return {
        "lane": "APP",
        "action": "fix_app_then_rerun_autopsy",
        "detail": "Mixed / stamp gaps still block clean LOGIC learning — APP first.",
    }


def build_shadow_loss_loop_report(
    *,
    day: str,
    data_root: Path | None = None,
    rebuild_autopsy: bool = True,
) -> dict[str, Any]:
    root = data_root or default_data_root()
    losers, autopsy = load_autopsy_losers(
        root, day=day, rebuild_if_missing=rebuild_autopsy
    )
    buckets = split_losers_by_class(losers)
    logic_set = ml_shadow_train_score_set(losers)
    excluded = excluded_from_ml_set(losers)
    shadow = run_shadow_on_logic(logic_set)
    entry_policy = run_entry_policy_counterfactual(losers)
    next_step = recommend_next_one_step(buckets, shadow)

    class_mix = {
        k: {
            "count": buckets[k].count,
            "pnl_gbp": round(buckets[k].pnl_gbp, 2),
        }
        for k in LOSS_CLASSES
    }
    app_effective_pnl = round(
        buckets["APP"].pnl_gbp + buckets["UNKNOWN"].pnl_gbp, 2
    )

    return {
        "day": day,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "data_root": str(root),
        "mode": "READ_ONLY",
        "constraints": {
            "places_orders": False,
            "lifts_a2": False,
            "claims_improvement_epoch": False,
            "instant_micro": "OFF",
            "unknown_treated_as": "APP",
            "ml_shadow_includes": "LOGIC_ONLY",
        },
        "operating_model": [
            "Recent losers → lifecycle autopsy (APP / LOGIC / UNKNOWN)",
            "APP (+ UNKNOWN) → tickets only; never ML edge claims",
            "LOGIC only → shadow re-score vs current profit_policy floor",
            "If APP dominate: fix APP + re-run autopsy after deploy",
            "If LOGIC dominate with clean stamps: one logic change OR ML veto learning",
            "NEVER mix APP+LOGIC in one ML train as edge",
        ],
        "cadence": {
            "daily": [
                "./scripts/run_daily_loss_autopsy.sh YYYY-MM-DD --with-review --with-shadow",
                (
                    "PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json "
                    ".venv/bin/python3 scripts/shadow_loss_loop.py --day YYYY-MM-DD"
                ),
            ],
            "note": (
                "Safe while bleed-locked / trading_paused. "
                "Do not unlock or POST /api/start from this loop."
            ),
        },
        "class_mix": class_mix,
        "app_effective": {
            "count": buckets["APP"].count + buckets["UNKNOWN"].count,
            "pnl_gbp": app_effective_pnl,
            "note": "UNKNOWN counted as APP until stamped",
        },
        "ml_excluded_count": len(excluded),
        "ml_shadow_eligible_count": len(logic_set),
        "shadow_counterfactual": shadow,
        "entry_policy_counterfactual": entry_policy,
        "next_one_step": next_step,
        "autopsy_summary": (autopsy or {}).get("summary") if autopsy else None,
        "sample_logic_deals": (shadow.get("samples") or [])[:12],
    }


def render_shadow_loss_loop_markdown(report: dict[str, Any]) -> str:
    day = report["day"]
    mix = report.get("class_mix") or {}
    shadow = report.get("shadow_counterfactual") or {}
    nxt = report.get("next_one_step") or {}
    lines: list[str] = [
        f"# Shadow Loss Loop — {day}",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Data root: `{report.get('data_root')}`",
        f"- Mode: **{report.get('mode')}** (no orders, no A2 lift, no improvement epoch)",
        "",
        "## Operating model",
        "",
    ]
    for bullet in report.get("operating_model") or []:
        lines.append(f"- {bullet}")
    lines.extend(["", "## Class mix (autopsy)", ""])
    lines.append("| Class | Count | PnL £ |")
    lines.append("|---|---:|---:|")
    for k in LOSS_CLASSES:
        row = mix.get(k) or {}
        lines.append(
            f"| {k} | {row.get('count', 0)} | {row.get('pnl_gbp', 0.0)} |"
        )
    ae = report.get("app_effective") or {}
    lines.append(
        f"| APP+UNKNOWN (ML-excluded) | {ae.get('count', 0)} | {ae.get('pnl_gbp', 0.0)} |"
    )
    lines.extend(
        [
            "",
            f"- ML shadow eligible (LOGIC only): **{report.get('ml_shadow_eligible_count')}**",
            f"- ML excluded (APP+UNKNOWN): **{report.get('ml_excluded_count')}**",
            "",
            "## Shadow counterfactual (LOGIC only)",
            "",
            f"- Score mode: `{shadow.get('score_mode')}`",
            f"- min_ml_probability floor: `{shadow.get('min_ml_probability')}`",
            f"- Model available on disk: `{shadow.get('model_available')}`",
            f"- LOGIC scored: {shadow.get('scored_count')} / {shadow.get('logic_count')} "
            f"(unscored={shadow.get('unscored_count')})",
            f"- Would-veto: **{shadow.get('would_veto_count')}** "
            f"(rate={shadow.get('would_veto_rate')}) "
            f"pnl_if_skipped_gbp={shadow.get('would_veto_pnl_gbp')}",
            f"- Would-lower-confidence: {shadow.get('would_lower_confidence_count')}",
            f"- By path style: `{shadow.get('by_path_style')}`",
            "",
            "### Sample LOGIC deals",
            "",
        ]
    )
    samples = report.get("sample_logic_deals") or shadow.get("samples") or []
    if not samples:
        lines.append("_No LOGIC losers in window._")
    else:
        lines.append(
            "| Deal | Epic | Path | PnL £ | Stamp ML | Would veto? | Reason |"
        )
        lines.append("|---|---|---|---:|---:|---|---|")
        for s in samples[:12]:
            lines.append(
                f"| `{s.get('deal_id')}` | {s.get('epic') or '?'} | "
                f"{s.get('path_style')} | {s.get('pnl_gbp')} | "
                f"{s.get('ml_score_at_entry')} | "
                f"{'YES' if s.get('would_veto') else 'no'} | {s.get('reason')} |"
            )
    ep = report.get("entry_policy_counterfactual") or {}
    if ep:
        lines.extend(
            [
                "",
                "## Entry-policy counterfactual (ALL losers — improvement check)",
                "",
                f"- {ep.get('note')}",
                f"- Floor `{ep.get('min_ml_probability')}` · "
                f"require_ml=`{ep.get('require_ml_probability')}`",
                f"- Would-veto: **{ep.get('would_veto_count')}** / {ep.get('losers')} "
                f"(rate={ep.get('would_veto_rate')}) "
                f"pnl_if_skipped_gbp={ep.get('would_veto_pnl_gbp')}",
                f"- By reason: `{ep.get('by_veto_reason')}`",
                f"- By class: `{ep.get('by_loss_class')}`",
                f"- APP structural targets: `{ep.get('app_structural_targets')}`",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## Recommended next ONE step",
            "",
            f"- Lane: **{nxt.get('lane')}**",
            f"- Action: `{nxt.get('action')}`",
            f"- Detail: {nxt.get('detail')}",
            "",
            "## Daily cadence",
            "",
        ]
    )
    for cmd in (report.get("cadence") or {}).get("daily") or []:
        lines.append(f"```bash\n{cmd}\n```")
    note = (report.get("cadence") or {}).get("note")
    if note:
        lines.append(f"\n_{note}_")
    lines.append("")
    return "\n".join(lines)


def write_shadow_loss_loop(
    *,
    day: str,
    data_root: Path | None = None,
    rebuild_autopsy: bool = True,
) -> tuple[Path, Path, dict[str, Any]]:
    root = data_root or default_data_root()
    report = build_shadow_loss_loop_report(
        day=day, data_root=root, rebuild_autopsy=rebuild_autopsy
    )
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"shadow_loss_loop_{day}.md"
    json_path = reports_dir / f"shadow_loss_loop_{day}.json"
    md_path.write_text(render_shadow_loss_loop_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return md_path, json_path, report
