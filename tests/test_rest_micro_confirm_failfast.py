"""REST confirm_deal must fail-fast on synthetic MICRO-* DualCore tokens."""

from __future__ import annotations

from unittest.mock import MagicMock

from ig_api.rest_client import IGRestClient


def _bare_client() -> IGRestClient:
    client = IGRestClient.__new__(IGRestClient)
    client.ensure_session = MagicMock()  # type: ignore[method-assign]
    client.request = MagicMock()  # type: ignore[method-assign]
    return client


def test_confirm_deal_refuses_synthetic_micro_token() -> None:
    client = _bare_client()
    out = client.confirm_deal("MICRO-DOW.IFM.IP-1784890000")
    assert out["terminal"] is True
    assert out["rejected"] is True
    assert out["reason"] == "invalid_deal_reference_pattern"
    client.request.assert_not_called()


def test_confirm_deal_refuses_epic_like_reference() -> None:
    client = _bare_client()
    out = client.confirm_deal("IX.D.DOW.IFM.IP")
    assert out["rejected"] is True
    assert out["reason"] == "invalid_deal_reference_pattern"
    client.request.assert_not_called()


def test_confirm_deal_refuses_empty_reference() -> None:
    client = _bare_client()
    out = client.confirm_deal("")
    assert out["rejected"] is True
    client.request.assert_not_called()
