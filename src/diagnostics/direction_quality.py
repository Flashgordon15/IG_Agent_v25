"""Direction-from-inception quality — was BUY/SELL wrong vs subsequent price?

Not a wire-swap detector. Measures whether the chosen side moved adverse from
entry→exit (priced losers), and whether that co-occurs with weak/fake ML stamps.

2026-07-24: 85 SELL vs 18 BUY losers; 91/93 priced losers adverse to side;
winners also SELL-heavy — so not a BUY↔SELL inversion bug. Real gaps were
threshold-constant 0.68 stamps and ml_prob<0.52 entries that slipped the floor.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any


def _f(x: Any) -> float | None:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def load_journal_price_rows(journal_path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not journal_path.is_file():
        return out
    with journal_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            did = str(row.get("DealID") or "").strip()
            if did:
                out[did] = row
    return out


def score_direction_quality(
    losers: list[dict[str, Any]],
    *,
    journal_by_deal: dict[str, dict[str, Any]] | None = None,
    journal_path: Path | None = None,
) -> dict[str, Any]:
    """Summarise whether loser directions were adverse to price from entry.

    Returns counts + a verdict string for autopsy / operator briefing.
    """
    jmap = journal_by_deal
    if jmap is None and journal_path is not None:
        jmap = load_journal_price_rows(journal_path)
    jmap = jmap or {}

    side_counts: Counter[str] = Counter()
    adverse = 0
    favorable_lost = 0
    no_prices = 0
    weak_ml_adverse = 0  # adverse + (ml < 0.52 or missing/threshold)
    sell_adverse_pts: list[float] = []
    buy_adverse_pts: list[float] = []

    for lc in losers:
        did = str(lc.get("deal_id") or "").strip()
        side = str(lc.get("direction") or lc.get("side") or "").upper()
        if side in ("BUY", "SELL"):
            side_counts[side] += 1
        j = jmap.get(did) or {}
        if not side:
            side = str(j.get("Direction") or "").upper()
            if side in ("BUY", "SELL"):
                side_counts[side] += 1
        entry = _f(j.get("EntryPrice") or lc.get("entry") or lc.get("entry_price"))
        exitp = _f(j.get("ExitPrice") or lc.get("exit") or lc.get("exit_price"))
        if entry is None or exitp is None or side not in ("BUY", "SELL"):
            no_prices += 1
            continue
        move = exitp - entry
        # Adverse: BUY into drop, SELL into rise
        is_adverse = (side == "BUY" and move < 0) or (side == "SELL" and move > 0)
        pts = abs(move)
        if is_adverse:
            adverse += 1
            if side == "SELL":
                sell_adverse_pts.append(pts)
            else:
                buy_adverse_pts.append(pts)
            ml = lc.get("ml_score_at_entry")
            weak = ml is None
            try:
                if ml is not None and float(ml) < 0.52:
                    weak = True
                from diagnostics.stamp_provenance import is_threshold_constant

                if ml is not None and is_threshold_constant(float(ml)):
                    weak = True
            except Exception:
                pass
            if weak:
                weak_ml_adverse += 1
        else:
            favorable_lost += 1

    priced = adverse + favorable_lost
    sell_n = int(side_counts.get("SELL", 0))
    buy_n = int(side_counts.get("BUY", 0))

    # Verdict: wire-swap would show ~50/50 with systematic against-trend; we saw
    # SELL-heavy day matching winner direction mix → not inversion.
    if priced == 0:
        verdict = "UNMEASURABLE — no entry/exit prices on losers"
    elif adverse / max(priced, 1) >= 0.9 and sell_n >= buy_n * 2:
        verdict = (
            "ADVERSE_TO_SIDE (expected for losers) + SELL-heavy day — "
            "NOT a BUY↔SELL wire inversion; focus ML floor / APP stack"
        )
    elif adverse / max(priced, 1) >= 0.9:
        verdict = (
            "ADVERSE_TO_SIDE (expected for losers) — direction matched the loss; "
            "not an inverted signal wire"
        )
    else:
        verdict = "MIXED — some losers closed with favorable price (check fees/stamps)"

    def _med(xs: list[float]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        return round(s[len(s) // 2], 2)

    return {
        "verdict": verdict,
        "losers": len(losers),
        "side_counts": dict(side_counts),
        "priced": priced,
        "adverse_to_side": adverse,
        "favorable_price_but_lost": favorable_lost,
        "no_prices": no_prices,
        "weak_ml_among_adverse": weak_ml_adverse,
        "sell_adverse_pts_median": _med(sell_adverse_pts),
        "buy_adverse_pts_median": _med(buy_adverse_pts),
        "wire_inversion_suspected": False,
        "note": (
            "Adverse-to-side is the definition of a directional loss. "
            "Wire inversion would need systematic side≠signal or against-trend "
            "with winners on the opposite side."
        ),
    }
