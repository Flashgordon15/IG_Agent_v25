"""Tests for broker epic resolver — spread bet vs CFD mapping."""

from __future__ import annotations

from unittest.mock import MagicMock

from execution.broker_epic_resolver import (
    detect_account_product_from_rest,
    resolve_account_product,
    resolve_hot_path_epics_from_config,
    resolve_order_epic,
)


def test_resolve_order_epic_spreadbet_maps_cfd_to_today():
    assert (
        resolve_order_epic("CS.D.EURUSD.CFD.IP", account_product="SPREADBET")
        == "CS.D.EURUSD.TODAY.IP"
    )
    assert (
        resolve_order_epic("CS.D.GBPUSD.CFD.IP", account_product="SPREADBET")
        == "CS.D.GBPUSD.TODAY.IP"
    )


def test_resolve_order_epic_cfd_unchanged():
    assert resolve_order_epic("CS.D.EURUSD.CFD.IP", account_product="CFD") == "CS.D.EURUSD.CFD.IP"


def test_resolve_account_product_reads_dual_core_override():
    cfg = {"dual_core": {"broker_account_product": "SPREADBET"}}
    assert resolve_account_product(cfg=cfg) == "SPREADBET"


def test_hot_path_epics_always_logical_cfd_keys():
    cfg = {
        "dual_core": {
            "hot_path_epics": ["CS.D.EURUSD.TODAY.IP", "CS.D.GBPUSD.TODAY.IP"],
            "hot_path_epics_cfd_fallback": [
                "CS.D.EURUSD.CFD.IP",
                "CS.D.GBPUSD.CFD.IP",
            ],
        }
    }
    assert resolve_hot_path_epics_from_config(cfg) == (
        "CS.D.EURUSD.CFD.IP",
        "CS.D.GBPUSD.CFD.IP",
    )


def test_detect_account_product_prefers_matching_account_id():
    rest = MagicMock()
    rest.account_id = "ABC123"
    rest._auth.tokens.account_id = "ABC123"
    rest._auth.tokens.raw = {
        "currentAccountId": "ABC123",
        "accounts": [
            {"accountId": "XYZ", "accountType": "CFD"},
            {"accountId": "ABC123", "accountType": "SPREADBET"},
        ],
    }
    assert detect_account_product_from_rest(rest) == "SPREADBET"


def test_detect_account_product_single_spreadbet_fallback():
    rest = MagicMock()
    rest.account_id = ""
    rest._auth.tokens.account_id = ""
    rest._auth.tokens.raw = {
        "accounts": [
            {"accountId": "A1", "accountType": "SPREADBET"},
        ],
    }
    assert detect_account_product_from_rest(rest) == "SPREADBET"
