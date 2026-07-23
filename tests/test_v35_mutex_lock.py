"""v35 race-condition remediation — order mutex, hard cap, 5s reconciler."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from execution.asymmetric_ioc_router import (
    dispatch_asymmetric_ioc_limit,
    get_asymmetric_ioc_router,
    plan_twap_fragments,
    reset_asymmetric_router_state_for_tests,
)
from execution.order_in_flight_mutex import (
    MUTEX_REJECT_LOG,
    get_order_mutex,
    hard_cap_blocks_entry,
    note_account_flat,
    note_account_open,
    pre_submit_hard_cap_gate,
    reset_order_mutex_for_tests,
    resolve_account_hard_open_cap,
    try_acquire_order_mutex,
)
from intelligence.target_engine import TargetSeekingEngine, reset_target_engine_for_tests
from system import agent_orchestration as ao


ACCT_CFD = "Z6BAH4"
ACCT_SB = "Z6BAH3"


@pytest.fixture(autouse=True)
def _reset_mutex_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from execution.streak_protection import reset_streak_protection_for_tests

    reset_order_mutex_for_tests()
    reset_asymmetric_router_state_for_tests()
    reset_streak_protection_for_tests()
    ao.reset_orchestrator_for_tests()
    reset_target_engine_for_tests()
    # Race tests must not inherit live desk streak tilt locks from v31-production.
    monkeypatch.setattr(
        "execution.streak_protection.check_streak_entry_allowed",
        lambda *_a, **_k: (True, "ok"),
    )
    yield
    reset_order_mutex_for_tests()
    reset_asymmetric_router_state_for_tests()
    reset_streak_protection_for_tests()
    ao.reset_orchestrator_for_tests()
    reset_target_engine_for_tests()


class _SlowRest:
    """Blocks inside place_otc so a second thread races the mutex."""

    account_id = ACCT_CFD

    def __init__(self, hold: threading.Event, release: threading.Event) -> None:
        self._hold = hold
        self._release = release
        self.calls = 0
        self._auth = SimpleNamespace(_tokens=SimpleNamespace(is_valid=True))
        self._open = 0

    def count_open_positions(self, epic=None):  # noqa: ANN001, ARG002
        return int(self._open)

    def open_positions(self):  # noqa: ANN001
        return []

    def place_otc_market_payload(self, payload):  # noqa: ANN001
        self.calls += 1
        self._hold.set()
        self._release.wait(timeout=5.0)
        self._open = 1
        return {"dealReference": f"REF-{self.calls}", "dealStatus": "ACCEPTED"}


def test_concurrent_dual_dispatch_second_gets_mutex_reject(monkeypatch, caplog) -> None:
    """Case 1: concurrent dual dispatch — only one order proceeds; second mutex-rejects."""
    import logging

    hold = threading.Event()
    release = threading.Event()
    rest = _SlowRest(hold, release)

    monkeypatch.setattr(
        "execution.asymmetric_ioc_router.auth_lane_ready",
        lambda _r: (True, ""),
    )
    monkeypatch.setattr(
        "execution.order_in_flight_mutex.hard_cap_blocks_entry",
        lambda *_a, **_k: (False, ""),
    )
    monkeypatch.setattr(
        "execution.maintenance_detachment.is_core_detached",
        lambda: False,
    )
    # Bypass size contraction / TWAP complexity.
    monkeypatch.setattr(
        "execution.asymmetric_ioc_router.apply_wr_size_contraction",
        lambda size, **_k: float(size),
    )
    monkeypatch.setattr(
        "execution.asymmetric_ioc_router.plan_twap_fragments",
        lambda size, **_k: [float(size)],
    )
    monkeypatch.setattr(
        "execution.live_broker_order_router.normalize_placement_distances",
        lambda *_a, **_k: (4.0, None, SimpleNamespace(min_points=1.0)),
    )

    results: list[dict] = []
    errors: list[BaseException] = []

    def _fire() -> None:
        try:
            results.append(
                dispatch_asymmetric_ioc_limit(
                    rest,
                    epic="IX.D.DOW.IFM.IP",
                    direction="BUY",
                    size=0.5,
                    bid=45000.0,
                    offer=45002.0,
                    stop_distance=4.0,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_fire, name="dispatch-1")
    t2 = threading.Thread(target=_fire, name="dispatch-2")

    with caplog.at_level(logging.INFO):
        # engine_log may not use logging — also assert via payload/reason
        t1.start()
        assert hold.wait(timeout=2.0), "first dispatch never entered place"
        t2.start()
        t2.join(timeout=2.0)
        release.set()
        t1.join(timeout=2.0)

    assert not errors
    assert len(results) == 2
    winners = [r for r in results if not r.get("vetoed") and r.get("dealReference")]
    losers = [
        r
        for r in results
        if r.get("vetoed") or r.get("reason") == "mutex_position_lock_active"
    ]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].get("reason") == "mutex_position_lock_active"
    assert MUTEX_REJECT_LOG in str(losers[0].get("rejection_reason") or "")
    assert rest.calls == 1
    assert get_asymmetric_ioc_router().order_in_flight is False
    assert get_order_mutex().is_locked(ACCT_CFD) is False


def test_hard_cap_blocks_when_open_ge_1_allows_when_flat() -> None:
    """Case 2: cap >=1 open → hard veto; flat book → allow once."""
    blocked, reason = hard_cap_blocks_entry(ACCT_CFD, open_count=1)
    assert blocked is True
    assert "account_hard_cap" in reason
    assert "Z6BAH4" in reason

    blocked0, reason0 = hard_cap_blocks_entry(ACCT_CFD, open_count=0)
    assert blocked0 is False
    assert reason0 == ""

    # Memory ledger bridges snapshot lag after a fill.
    note_account_flat(ACCT_CFD)
    note_account_open(ACCT_CFD, delta=1)
    blocked_mem, reason_mem = hard_cap_blocks_entry(ACCT_CFD, open_count=0)
    assert blocked_mem is True
    assert "account_hard_cap" in reason_mem
    note_account_flat(ACCT_CFD)

    # SB is independently hard-capped at 1 (no bleed from CFD, same ceiling).
    sb_blocked, sb_reason = hard_cap_blocks_entry(ACCT_SB, open_count=1)
    assert sb_blocked is True
    assert "account_hard_cap" in sb_reason
    assert "Z6BAH3" in sb_reason
    sb_flat, sb_flat_reason = hard_cap_blocks_entry(ACCT_SB, open_count=0)
    assert sb_flat is False
    assert sb_flat_reason == ""

    from system.engine_lane import ENGINE_CFD_SNIPER, ENGINE_SB_SENTINEL, count_cap_for_engine

    cfg = {
        "engine_position_caps": {"cfd_sniper": 99, "sb_sentinel": 10},
        "max_open_positions": None,
    }
    # Soft engine caps may differ; runtime HARD_OPEN_CAP is 1 for both accounts.
    assert count_cap_for_engine(ENGINE_CFD_SNIPER, cfg) == 1
    assert resolve_account_hard_open_cap(ACCT_SB) == 1


def test_hard_cap_account_forces_15m_trend_lock(monkeypatch) -> None:
    """Hard-capped CFD+SB must not mean-revert against 15m trend."""
    import runtime.dual_core_execution as dce

    monkeypatch.setenv("IG_ACCOUNT_ID", ACCT_CFD)
    assert dce.is_core_b_satellite_uncoupled() is False
    monkeypatch.setenv("IG_ACCOUNT_ID", ACCT_SB)
    # SB hard-cap=1 also forces 15m trend coupling (same wrong-way pattern).
    assert dce.is_core_b_satellite_uncoupled() is False


def test_ambiguous_over_5s_orchestrator_clears_mutex_and_reconciles(
    monkeypatch,
) -> None:
    """Case 3: ambiguous >5s → orchestrator clears mutex + reconciles positions."""
    assert try_acquire_order_mutex(ACCT_CFD, epic="IX.D.DOW.IFM.IP", source="test")
    mux = get_order_mutex()
    assert mux.is_locked(ACCT_CFD) is True
    assert mux.order_in_flight is True

    # Backdate acquire so age > 5.0s without sleeping wall-clock.
    with mux._lock:
        mux._in_flight[ACCT_CFD] = time.monotonic() - 5.5

    mock_client = MagicMock()
    mock_client.count_open_positions.return_value = 1
    mock_client.count_open_positions_live.return_value = 1
    mock_registry = MagicMock()
    mock_registry.get_client_for_account.return_value = mock_client
    monkeypatch.setattr(
        "runtime.session_registry.get_session_registry",
        lambda: mock_registry,
    )
    monkeypatch.setattr(
        "system.credentials_loader.try_load_credentials",
        lambda: MagicMock(),
    )

    out = ao.maybe_reconcile_ambiguous_order_mutex(timeout_sec=5.0)
    assert out is not None
    assert out["action"] == "ambiguous_order_mutex_reconcile"
    assert mux.is_locked(ACCT_CFD) is False
    assert mux.order_in_flight is False

    status = ao.get_orchestrator_status()
    assert status["mutex_reconcile"] is not None
    accounts = status["mutex_reconcile"]["accounts"]
    assert accounts[0]["account_id"] == ACCT_CFD
    assert accounts[0]["mutex_cleared"] is True
    assert accounts[0]["broker_opens"] == 1
    assert status["order_mutex"]["order_in_flight"] is False
    mock_client.count_open_positions_live.assert_called()


def test_stale_snapshot_undercount_cannot_allow_second_submit(monkeypatch) -> None:
    """Stale flat snapshot must not override memory/disk ledger after a fill."""
    note_account_flat(ACCT_CFD)
    note_account_open(ACCT_CFD, delta=1)

    monkeypatch.setattr(
        "execution.order_in_flight_mutex.broker_open_count_authoritative",
        lambda *_a, **_k: 0,  # stale undercount
    )
    blocked, reason = hard_cap_blocks_entry(ACCT_CFD, open_count=None)
    assert blocked is True
    assert "account_hard_cap" in reason
    note_account_flat(ACCT_CFD)


def test_pre_submit_clears_stale_ledger_when_raw_broker_flat() -> None:
    """Soak stall vector: mem/disk=1 while broker_raw=0 must re-arm (not permanent block)."""
    note_account_flat(ACCT_CFD)
    note_account_open(ACCT_CFD, delta=1)
    from execution.order_in_flight_mutex import (
        arm_entry_quarantine,
        disk_open_count,
        memory_open_count,
    )

    arm_entry_quarantine(ACCT_CFD)
    assert memory_open_count(ACCT_CFD) >= 1
    assert disk_open_count(ACCT_CFD) >= 1

    rest = MagicMock()
    rest.count_open_positions_live.return_value = 0
    rest.count_open_positions.return_value = 0
    ok, reason, reserved = pre_submit_hard_cap_gate(
        ACCT_CFD, rest=rest, source="stale_ledger_clear", mux_already_held=False
    )
    assert ok is True, reason
    assert reserved is True
    assert memory_open_count(ACCT_CFD) >= 1  # reserved for this submit
    # Rollback reservation for clean teardown.
    from execution.order_in_flight_mutex import release_pre_submit_reservation

    release_pre_submit_reservation(ACCT_CFD, filled=False)
    note_account_flat(ACCT_CFD)


def test_pre_submit_blocks_when_snapshot_zero_but_raw_one() -> None:
    """Integration: stale snapshot undercount=0 while raw=1 → second submit blocked."""
    note_account_flat(ACCT_CFD)
    rest = MagicMock()
    # Live SoT shows an open; snapshot helpers would say 0.
    rest.count_open_positions_live.return_value = 1
    rest.count_open_positions.return_value = 0

    ok, reason, reserved = pre_submit_hard_cap_gate(
        ACCT_CFD, rest=rest, source="raw_over_snapshot", mux_already_held=False
    )
    assert ok is False
    assert reserved is False
    assert "account_hard_cap" in reason
    note_account_flat(ACCT_CFD)


def test_pre_submit_gate_rejects_when_raw_broker_open_ge_1() -> None:
    """Last-line gate: raw broker open>=1 → veto even with flat memory."""
    note_account_flat(ACCT_CFD)
    rest = MagicMock()
    rest.count_open_positions_live.return_value = 1
    rest.count_open_positions.return_value = 1
    ok, reason, reserved = pre_submit_hard_cap_gate(
        ACCT_CFD, rest=rest, source="test", mux_already_held=False
    )
    assert ok is False
    assert reserved is False
    assert "account_hard_cap" in reason
    assert get_order_mutex().is_locked(ACCT_CFD) is False


def test_pre_submit_gate_blocks_second_submit_under_held_mutex() -> None:
    """TWAP/stack: second pre_submit under same mutex hold is rejected."""
    note_account_flat(ACCT_CFD)
    assert try_acquire_order_mutex(ACCT_CFD, epic="IX.D.DOW.IFM.IP", source="test")
    rest = MagicMock()
    rest.count_open_positions_live.return_value = 0  # broker lag
    rest.count_open_positions.return_value = 0

    ok1, reason1, _ = pre_submit_hard_cap_gate(
        ACCT_CFD, rest=rest, source="clip1", mux_already_held=True
    )
    assert ok1 is True, reason1

    ok2, reason2, _ = pre_submit_hard_cap_gate(
        ACCT_CFD, rest=rest, source="clip2", mux_already_held=True
    )
    assert ok2 is False
    assert "entry_posted" in reason2 or "account_hard_cap" in reason2


def test_twap_hard_cap_account_never_fragments() -> None:
    """Z6BAH4 must not TWAP-split (each clip is forceOpen)."""
    frags = plan_twap_fragments(2.5, epic="IX.D.DOW.IFM.IP", account_id=ACCT_CFD)
    assert frags == [2.5]

    # Uncapped / unknown account may still fragment when size >> min lot.
    # (SB account is not in HARD_OPEN_CAP_BY_ACCOUNT.)
    frags_sb = plan_twap_fragments(
        0.5, epic="IX.D.DOW.IFM.IP", account_id=ACCT_SB
    )
    assert frags_sb == [0.5]


def test_capital_preservation_no_false_halt_on_inflated_journal() -> None:
    """Inflated learning-store P&L alone must not engage capital preservation."""
    engine = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
    store = MagicMock()
    store.sum_daily_pnl.return_value = 2500.0  # phantom / cascade journal
    engine.bind_store(store)

    rest = MagicMock()
    rest.maybe_refresh_account_summary.return_value = {"balance": 10360.0}
    engine.bind_rest_client(rest)
    engine.mark_session_start(10000.0)  # broker session +£360

    # Patch daily_loss path to return inflated store figure.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "system.daily_loss_policy.effective_daily_pnl",
            lambda *_a, **_k: 2500.0,
        )
        snap = engine.refresh(force_balance=True)

    assert snap["capital_preservation"] is False
    assert engine.capital_preservation is False


def test_capital_preservation_trips_when_broker_confirms_target() -> None:
    """Broker balance-delta at target must still engage capital preservation."""
    reset_target_engine_for_tests()
    engine = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
    engine.bind_store(MagicMock())
    rest = MagicMock()
    rest.maybe_refresh_account_summary.return_value = {"balance": 11050.0}
    engine.bind_rest_client(rest)
    engine.mark_session_start(10000.0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "system.daily_loss_policy.effective_daily_pnl",
            lambda *_a, **_k: 50.0,
        )
        snap = engine.refresh(force_balance=True)
    assert snap["capital_preservation"] is True
    reset_target_engine_for_tests()


def test_gate_exception_is_fail_closed_not_swallowed(monkeypatch) -> None:
    """Hard-cap gate errors must reject — never fall through to POST."""
    from ig_api.exceptions import IGOrderError
    from ig_api.rest_client import IGRestClient

    class _Client(IGRestClient):
        def __init__(self) -> None:
            self.account_id = ACCT_CFD
            self._base = "https://example.invalid"

        def ensure_session(self) -> None:
            return None

        def _auth_headers(self, *_a, **_k):  # noqa: ANN001
            return {}

        def request(self, *a, **k):  # noqa: ANN001
            raise AssertionError("POST must not fire when gate errors")

        def record_rest_success(self, *_a, **_k) -> None:
            return None

    monkeypatch.setattr(
        "execution.maintenance_detachment.is_core_detached",
        lambda: False,
    )
    monkeypatch.setattr(
        "execution.ig_rest_traffic_governor.consume_positions_otc_transmit_slot",
        lambda **_k: (True, ""),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("flock_or_count_failed")

    monkeypatch.setattr(
        "execution.order_in_flight_mutex.pre_submit_hard_cap_gate",
        _boom,
    )

    client = _Client()
    with pytest.raises(IGOrderError) as ei:
        client.place_otc_market_payload(
            {
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "orderType": "MARKET",
                "maxSlippage": 2,
            }
        )
    assert "gate_error" in str(ei.value) or "account_hard_cap" in str(ei.value)


def test_sync_ledger_clears_when_broker_flat() -> None:
    from execution.order_in_flight_mutex import (
        memory_open_count,
        note_account_open,
        sync_hard_cap_ledger_with_broker,
    )

    note_account_open(ACCT_CFD, delta=1)
    assert memory_open_count(ACCT_CFD) >= 1
    rest = MagicMock()
    rest.count_open_positions_live.return_value = 0
    rest.count_open_positions.return_value = 0
    n = sync_hard_cap_ledger_with_broker(ACCT_CFD, rest=rest)
    assert n == 0
    assert memory_open_count(ACCT_CFD) == 0


def test_sync_does_not_clear_ledger_when_live_count_unavailable() -> None:
    """Failed/None live count must not wipe ledger (stale-zero cascade vector)."""
    from execution.order_in_flight_mutex import (
        memory_open_count,
        note_account_open,
        sync_hard_cap_ledger_with_broker,
    )

    note_account_open(ACCT_CFD, delta=1)
    assert memory_open_count(ACCT_CFD) >= 1
    rest = MagicMock()
    # Live helper exists but fails — must NOT fall back to stale 0 and clear.
    rest.count_open_positions_live.side_effect = RuntimeError("rate_limited")
    rest.count_open_positions.return_value = 0
    n = sync_hard_cap_ledger_with_broker(ACCT_CFD, rest=rest)
    assert n is None
    assert memory_open_count(ACCT_CFD) >= 1


def test_delete_with_body_rewritten_to_post_method_header() -> None:
    """IG drops DELETE bodies — request() must POST with _method: DELETE."""
    from ig_api.rest_client import IGRestClient

    client = IGRestClient.__new__(IGRestClient)
    client.account_id = ACCT_CFD
    client.timeout_seconds = 5
    client.max_retries = 1
    client.retry_delay_seconds = 0
    client._base = "https://demo-api.ig.com/gateway/deal"
    client._session_path_protected = lambda _p: True  # type: ignore[method-assign]
    client.proactive_refresh_if_needed = lambda: None  # type: ignore[method-assign]
    client.record_rest_success = lambda *_a, **_k: None  # type: ignore[method-assign]

    captured: dict[str, Any] = {}

    class _Sess:
        def request(self, method, url, timeout=None, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kwargs.get("headers") or {}
            captured["json"] = kwargs.get("json")

            class _R:
                status_code = 200
                text = '{"dealReference":"REF"}'

                def json(self):
                    return {"dealReference": "REF"}

            return _R()

    client._session = _Sess()  # type: ignore[attr-defined]

    # Bypass budget/chaos layers by stubbing heavy imports via env — call request
    # with auth_required=False so we only exercise the DELETE rewrite.
    r = client.request(
        "DELETE",
        "/positions/otc",
        auth_required=False,
        headers={"VERSION": "1"},
        json={
            "dealId": "DIAAAAXTEST",
            "direction": "SELL",
            "size": 0.5,
            "orderType": "MARKET",
        },
    )
    assert r.status_code == 200
    assert captured["method"] == "POST"
    assert captured["headers"].get("_method") == "DELETE"
    assert captured["json"]["dealId"] == "DIAAAAXTEST"


def test_net_close_refused_when_no_opposite_even_with_ig_row() -> None:
    """Hard-cap: DELETE fail must NOT POST forceOpen=false without opposite side.

    Prior bug: ``opposite_n <= 0 and ig_row is None`` allowed spawn whenever the
    deal row was still visible — cascade vector after validation.null-not-allowed.
    """
    from ig_api.exceptions import IGOrderError
    from ig_api.rest_client import IGRestClient

    client = IGRestClient.__new__(IGRestClient)
    client.account_id = ACCT_CFD
    client.ensure_session = lambda: None  # type: ignore[method-assign]
    client._auth_headers = lambda *_a, **_k: {}  # type: ignore[method-assign]
    # Stale SELL row while book is already all BUY (post-spawn / race).
    client.find_open_position = lambda _deal: {  # type: ignore[method-assign]
        "position": {"direction": "SELL", "size": 0.5, "currency": "USD", "dealId": "DIAAAAXTESTSELL"},
        "market": {"epic": "IX.D.DOW.IFM.IP"},
    }
    only_buys = [
        {
            "position": {"direction": "BUY", "size": 0.5, "dealId": "DIAAAAXTESTBUY1"},
            "market": {"epic": "IX.D.DOW.IFM.IP"},
        }
    ]
    client.fetch_open_positions = lambda _epic=None: only_buys  # type: ignore[method-assign]
    client.open_positions = lambda: only_buys  # type: ignore[method-assign]
    client.fetch_market_constraints = lambda *_a, **_k: {"market_status": "TRADEABLE"}  # type: ignore[method-assign]
    client.normalize_order_params = (  # type: ignore[method-assign]
        lambda epic, size=None, stop_distance=None, limit_distance=None, currency_code=None: (
            float(size or 0.5),
            float(stop_distance or 6),
            float(limit_distance or 12),
            str(currency_code or "USD"),
        )
    )

    class _Resp:
        status_code = 400
        text = '{"errorCode":"validation.null-not-allowed.request"}'

        def json(self):
            return {"errorCode": "validation.null-not-allowed.request"}

    posts: list[dict] = []

    def _request(method, path, **kwargs):
        if str(method).upper() == "POST":
            posts.append(kwargs.get("json") or {})
        return _Resp()

    client.request = _request  # type: ignore[method-assign]

    with pytest.raises(IGOrderError) as ei:
        client._do_close_position(
            "DIAAAAXTESTSELL",
            direction="SELL",
            size=0.5,
            epic="IX.D.DOW.IFM.IP",
            currency_code="USD",
            verify=False,
            skip_confirm=True,
        )
    assert posts == []  # never POSTed forceOpen=false spawn
    msg = str(ei.value)
    assert ("net_close_refused" in msg) or ("net-close disabled" in msg)
