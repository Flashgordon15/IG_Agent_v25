"""Hard IG minimum deal size floors."""

from __future__ import annotations

import pytest

from execution.size_floors import (
    hard_min_deal_size,
    effective_min_deal_size,
    SPREADBET_MIN_DEAL_BY_EPIC,
)


@pytest.mark.parametrize(
    "epic,expected",
    [
        ("CS.D.CFPGOLD.CFP.IP", 10.0),
        ("IX.D.NIKKEI.IFM.IP", 0.5),
        ("IX.D.DOW.IFM.IP", 0.5),
    ],
)
def test_spreadbet_hard_mins(epic: str, expected: float, monkeypatch) -> None:
    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    assert hard_min_deal_size(epic) == expected


def test_effective_min_never_below_hard(monkeypatch) -> None:
    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    assert effective_min_deal_size("CS.D.CFPGOLD.CFP.IP", rest_min=1.0) == 10.0
    assert effective_min_deal_size("IX.D.DOW.IFM.IP", rest_min=0.3) == 0.5


def test_gold_in_authoritative_map() -> None:
    assert SPREADBET_MIN_DEAL_BY_EPIC["CS.D.CFPGOLD.CFP.IP"] == 10.0
