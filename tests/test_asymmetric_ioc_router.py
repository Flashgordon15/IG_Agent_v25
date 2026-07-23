"""Native IG OTC MARKET asymmetric router — maxSlippage, auth gate, reject backoff."""

from __future__ import annotations

import pytest

from execution.asymmetric_ioc_router import (
    auth_lane_ready,
    build_ig_otc_market_payload,
    compute_max_slippage,
    dispatch_asymmetric_ioc_limit,
    reset_asymmetric_router_state_for_tests,
    touch_level,
    _note_slippage_rejection,
)
from ig_api.exceptions import IGOrderError


@pytest.fixture(autouse=True)
def _clear_router_state():
    reset_asymmetric_router_state_for_tests()
    yield
    reset_asymmetric_router_state_for_tests()


def test_touch_level_aggressive():
    assert touch_level("BUY", 100.0, 100.5) == 100.5
    assert touch_level("SELL", 100.0, 100.5) == 100.0


def test_max_slippage_half_spread_integer():
    # spread=2.0 → 0.5*2 = 1
    assert compute_max_slippage(45000.0, 45002.0) == 1
    # spread=4 → 2
    assert compute_max_slippage(100.0, 104.0) == 2
    # spread=1 → round(0.5)=0 → floor 1
    assert compute_max_slippage(10.0, 11.0) == 1


def test_max_slippage_invalid_book_floors_one():
    assert compute_max_slippage(10.0, 10.0) == 1
    assert compute_max_slippage(10.0, 9.0) == 1


def test_payload_is_native_ig_market_no_tif():
    payload = build_ig_otc_market_payload(
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        stop_distance=4.0,
        max_slippage=1,
        limit_distance=8.0,
        currency_code="GBP",
    )
    assert payload["orderType"] == "MARKET"
    assert payload["maxSlippage"] == 1
    assert "timeInForce" not in payload
    assert "FILL_OR_KILL" not in str(payload)
    assert "IMMEDIATE_OR_CANCEL" not in str(payload)
    assert payload["limitDistance"] == 8.0
    assert payload["forceOpen"] is True


def test_payload_omits_limit_when_none():
    payload = build_ig_otc_market_payload(
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        size=0.5,
        stop_distance=4.0,
        max_slippage=2,
    )
    assert "limitDistance" not in payload
    assert payload["direction"] == "SELL"


class _AuthTok:
    def __init__(self, valid: bool = True):
        self.cst = "CST" if valid else ""
        self.security_token = "XST" if valid else ""

    @property
    def is_valid(self) -> bool:
        return bool(self.cst and self.security_token)


class _AuthMgr:
    def __init__(self, valid: bool = True):
        self._tokens = _AuthTok(valid) if valid else None

    @property
    def tokens(self):
        return self._tokens


class _RestPayload:
    def __init__(self, *, auth_ready: bool = True, reject: bool = False):
        self._auth = _AuthMgr(valid=auth_ready)
        self._session_refresh_in_progress = False
        self._token_eviction_in_progress = False
        self._auth_ready = auth_ready
        self.reject = reject
        self.calls: list[dict] = []

    def auth_ready_for_hot_path(self) -> bool:
        if self._session_refresh_in_progress or self._token_eviction_in_progress:
            return False
        return self._auth_ready and bool(
            self._auth._tokens and self._auth._tokens.is_valid
        )

    def place_otc_market_payload(self, payload: dict):
        self.calls.append(dict(payload))
        if self.reject:
            raise IGOrderError(
                "order rejected: maxSlippage exceeded / pricing mismatch",
                status_code=400,
            )
        return {"dealReference": "REF-MKT", "orderType": "MARKET"}


def test_dispatch_posts_market_with_max_slippage():
    rest = _RestPayload()
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
        limit_distance=8.0,
        cfg={"asymmetric_ioc_routing": {"enabled": True}},
    )
    assert out["dealReference"] == "REF-MKT"
    assert len(rest.calls) == 1
    body = rest.calls[0]
    assert body["orderType"] == "MARKET"
    assert body["maxSlippage"] == 1
    assert "timeInForce" not in body
    assert body["direction"] == "BUY"
    assert body["size"] == 0.5


def test_dispatch_floors_stop_to_broker_min_stop_distance():
    """Regression: DOW micro 4pt stop < IG min 6 → ATTACHED_ORDER_LEVEL_ERROR."""

    class _RestMinStop(_RestPayload):
        def fetch_market_constraints(self, epic: str) -> dict:
            return {"min_stop_distance": 6.0}

    rest = _RestMinStop()
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        size=0.5,
        bid=52008.0,
        offer=52012.0,
        stop_distance=4.0,
        limit_distance=10.0,
        cfg={"asymmetric_ioc_routing": {"enabled": True}},
    )
    assert out["dealReference"] == "REF-MKT"
    body = rest.calls[0]
    assert body["stopDistance"] == 6.0
    assert body["limitDistance"] == 10.0


def test_dispatch_sell_uses_market_not_limit_level():
    rest = _RestPayload()
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        size=0.5,
        bid=45000.0,
        offer=45004.0,
        stop_distance=4.0,
    )
    assert out["dealReference"] == "REF-MKT"
    assert rest.calls[0]["orderType"] == "MARKET"
    assert rest.calls[0]["maxSlippage"] == 2
    assert "level" not in rest.calls[0]


def test_auth_invalid_tokens_fail_closed_no_http():
    rest = _RestPayload(auth_ready=False)
    rest._auth = _AuthMgr(valid=False)
    rest._auth_ready = False
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert out["vetoed"] is True
    assert out["dealReference"] is None
    assert rest.calls == []


def test_auth_refresh_in_progress_vetoes():
    rest = _RestPayload()
    rest._session_refresh_in_progress = True
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert out["vetoed"] is True
    assert "auth" in out["reason"] or "refresh" in out["reason"] or out["reason"] == "auth_lane_not_ready"
    assert rest.calls == []


def test_auth_eviction_in_progress_vetoes():
    rest = _RestPayload()
    rest._token_eviction_in_progress = True
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert out["vetoed"] is True
    assert rest.calls == []


def test_auth_lane_ready_fallback_inspects_auth_manager():
    class RestNoMethod:
        def __init__(self):
            self._auth = _AuthMgr(valid=True)
            self._session_refresh_in_progress = False
            self._token_eviction_in_progress = False

    ok, reason = auth_lane_ready(RestNoMethod())
    assert ok is True
    assert reason == ""

    class RestBad:
        def __init__(self):
            self._auth = _AuthMgr(valid=False)
            self._session_refresh_in_progress = False
            self._token_eviction_in_progress = False

    ok2, reason2 = auth_lane_ready(RestBad())
    assert ok2 is False
    assert reason2


def test_invalid_book_veto():
    rest = _RestPayload()
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=0.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert out["vetoed"] is True
    assert out["reason"] == "invalid_touch_book"
    assert rest.calls == []


def test_five_rejects_in_30s_arms_60s_backoff():
    rest = _RestPayload(reject=True)
    for _ in range(5):
        with pytest.raises(IGOrderError):
            dispatch_asymmetric_ioc_limit(
                rest,
                epic="IX.D.DOW.IFM.IP",
                direction="BUY",
                size=0.5,
                bid=45000.0,
                offer=45002.0,
                stop_distance=4.0,
            )
    # 6th attempt must be vetoed by backoff (no HTTP)
    rest.reject = False
    rest.calls.clear()
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert out["vetoed"] is True
    assert out["reason"] == "rejection_backoff"
    assert rest.calls == []


def test_backoff_helper_records_slippage_markers():
    for _ in range(5):
        _note_slippage_rejection()
    rest = _RestPayload()
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert out["reason"] == "rejection_backoff"


def test_success_clears_consecutive_reject_streak():
    rest = _RestPayload(reject=True)
    for _ in range(3):
        with pytest.raises(IGOrderError):
            dispatch_asymmetric_ioc_limit(
                rest,
                epic="IX.D.DOW.IFM.IP",
                direction="BUY",
                size=0.5,
                bid=45000.0,
                offer=45002.0,
                stop_distance=4.0,
            )
    rest.reject = False
    ok = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert ok.get("dealReference") == "REF-MKT"
    # After success, 4 more rejects should NOT yet arm backoff (need 5 consecutive)
    rest.reject = True
    for _ in range(4):
        with pytest.raises(IGOrderError):
            dispatch_asymmetric_ioc_limit(
                rest,
                epic="IX.D.DOW.IFM.IP",
                direction="BUY",
                size=0.5,
                bid=45000.0,
                offer=45002.0,
                stop_distance=4.0,
            )
    rest.reject = False
    rest.calls.clear()
    still = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
    )
    assert still.get("dealReference") == "REF-MKT"


def test_place_market_order_fallback_receives_max_slippage():
    class RestMarketOnly:
        def __init__(self):
            self._auth = _AuthMgr(True)
            self.kwargs = None

        def auth_ready_for_hot_path(self) -> bool:
            return True

        def place_market_order(self, **kwargs):
            self.kwargs = kwargs
            return {"dealReference": "REF-FB"}

    rest = RestMarketOnly()
    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45004.0,
        stop_distance=4.0,
    )
    assert out["dealReference"] == "REF-FB"
    assert rest.kwargs["max_slippage"] == 2
    assert rest.kwargs["force_market"] is True
    assert "time_in_force" not in rest.kwargs


def test_no_exchange_tif_keys_in_dispatch_path():
    rest = _RestPayload()
    dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=4.0,
        cfg={"asymmetric_ioc_routing": {"time_in_force": "FILL_OR_KILL"}},
    )
    dumped = str(rest.calls[0])
    assert "timeInForce" not in dumped
    assert "FILL_OR_KILL" not in dumped
