"""Asset policy alignment: Nikkei hot-path excluded; Gold remains rank-eligible.

Does not invent a contradictory global ALLOWED_ASSETS list — asserts existing
dual_core helpers / config keys only.
"""

from __future__ import annotations

import json
from pathlib import Path

from runtime.dual_core_execution import epic_allowed_on_hot_path, reset_dual_core_for_tests

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config" / "config_v31_demo_throughput.json"

NIKKEI = "IX.D.NIKKEI.IFM.IP"
GOLD = "CS.D.CFPGOLD.CFP.IP"
EURUSD = "CS.D.EURUSD.CFD.IP"
DOW = "IX.D.DOW.IFM.IP"
FTSE = "IX.D.FTSE.IFM.IP"


def _dual_from_demo() -> dict:
    raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    dual = dict(raw.get("dual_core") or {})
    return dual


def test_nikkei_excluded_gold_eur_remain_ranked_candidates():
    dual = _dual_from_demo()
    excluded = {str(e) for e in (dual.get("exclude_from_hot_path") or [])}
    ranked = [str(e) for e in (dual.get("ranked_candidate_epics") or [])]

    assert NIKKEI in excluded
    assert GOLD in ranked
    assert EURUSD in ranked
    assert DOW in ranked
    assert FTSE in ranked
    # Must not shrink to Wall St + FTSE only
    assert set(ranked) != {DOW, FTSE}
    assert GOLD in ranked and EURUSD in ranked


def test_epic_allowed_on_hot_path_blocks_nikkei_keeps_dow():
    reset_dual_core_for_tests()
    try:
        dual = _dual_from_demo()
        cfg = {"dual_core": dual}
        assert epic_allowed_on_hot_path(NIKKEI, cfg) is False
        assert epic_allowed_on_hot_path(DOW, cfg) is True
    finally:
        reset_dual_core_for_tests()
