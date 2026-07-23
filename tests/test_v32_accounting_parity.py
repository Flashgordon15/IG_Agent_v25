"""v32 accounting ledger — expanded journal columns and engine metadata."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from diagnostics.performance_journal import (
    _HEADER,
    _JournalEvent,
    _append_row,
    _ensure_journal_header,
    _resolve_event_metadata,
    enable_sync_mode_for_tests,
    journal_path,
    record_trade_close,
    reset_performance_journal_for_tests,
    upsert_journal_cash_close,
)
from system.engine_lane import (
    DEFAULT_ACCOUNT_CFD,
    DEFAULT_ACCOUNT_SB,
    ENGINE_ORIGIN_CFD,
    ENGINE_ORIGIN_SB,
    ENGINE_CFD_SNIPER,
    ENGINE_SB_SENTINEL,
    engine_position_cap,
    global_max_open_positions,
    resolve_journal_metadata,
)


@pytest.fixture(autouse=True)
def _clean_journal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "IG_V32_DUAL_PORT",
        "IG_ENGINE_ORIGIN",
        "IG_ACCOUNT_ID",
        "IG_ACCOUNT_SCOPE",
        "IG_API_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _clean_journal() -> None:
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    yield
    reset_performance_journal_for_tests()


def test_journal_header_includes_v32_accounting_columns() -> None:
    assert "AccountID" in _HEADER
    assert "ProductType" in _HEADER
    assert "EngineOrigin" in _HEADER
    assert _HEADER.index("AccountID") == 8
    assert _HEADER.index("ProductType") == 9
    assert _HEADER.index("EngineOrigin") == 10


def test_record_trade_close_writes_account_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr("diagnostics.performance_journal.journal_path", lambda: path)
    record_trade_close(
        deal_id="DIAAAACFD001",
        direction="BUY",
        entry_price=100.0,
        exit_price=101.0,
        realized_pnl_gbp=2.5,
        engine_id=ENGINE_CFD_SNIPER,
        account_id=DEFAULT_ACCOUNT_CFD,
        product_type="CFD",
        engine_origin=ENGINE_ORIGIN_CFD,
    )
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 1
    row = rows[0]
    assert row["DealID"] == "DIAAAACFD001"
    assert row["RealizedPnL_GBP"] == "2.5"
    assert row["AccountID"] == DEFAULT_ACCOUNT_CFD
    assert row["ProductType"] == "CFD"
    assert row["EngineOrigin"] == ENGINE_ORIGIN_CFD


def test_journal_metadata_defaults_from_engine_lane() -> None:
    cfg = {
        "dual_core": {"broker_account_product": "SPREADBET"},
        "engine_lanes": {
            ENGINE_SB_SENTINEL: {
                "account_id": DEFAULT_ACCOUNT_SB,
                "product_type": "SPREADBET",
                "engine_origin": ENGINE_ORIGIN_SB,
            }
        },
    }
    meta = resolve_journal_metadata(engine_id=ENGINE_SB_SENTINEL, cfg=cfg)
    assert meta["account_id"] == DEFAULT_ACCOUNT_SB
    assert meta["product_type"] == "SPREADBET"
    assert meta["engine_origin"] == ENGINE_ORIGIN_SB


def test_legacy_csv_header_expanded_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "daily_journal.csv"
    legacy_header = [
        "Timestamp",
        "DealID",
        "Direction",
        "EntryPrice",
        "ExitPrice",
        "RealizedPnL_GBP",
        "ClosingFillRate",
        "ActiveSlipMultiplier",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(legacy_header)
        w.writerow(
            [
                "2026-07-22T10:00:00Z",
                "DIAAAALEG001",
                "SELL",
                "52000",
                "51990",
                "5.0",
                "0.8",
                "0.5",
            ]
        )
    _ensure_journal_header(path)
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0] == _HEADER
    assert rows[1][1] == "DIAAAALEG001"
    assert rows[1][5] == "5.0"
    assert rows[1][8] == ""
    assert rows[1][9] == ""
    assert rows[1][10] == ""


def test_journal_event_dataclass_and_upsert_type_safety(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr("diagnostics.performance_journal.journal_path", lambda: path)
    ev = _JournalEvent(
        kind="trade_close",
        ts=1_700_000_000.0,
        deal_id="DIAAAAUPS001",
        direction="BUY",
        entry=1.0,
        exit=2.0,
        pnl_gbp=3.0,
        account_id=DEFAULT_ACCOUNT_SB,
        product_type="SPREADBET",
        engine_origin=ENGINE_ORIGIN_SB,
    )
    meta = _resolve_event_metadata(
        account_id=ev.account_id,
        product_type=ev.product_type,
        engine_origin=ev.engine_origin,
    )
    assert meta["engine_origin"] == ENGINE_ORIGIN_SB
    _append_row(ev)
    upsert_journal_cash_close(
        deal_id="DIAAAAUPS002",
        direction="BUY",
        entry_price=10.0,
        exit_price=11.0,
        realized_pnl_gbp=4.0,
        engine_id=ENGINE_SB_SENTINEL,
    )
    text = path.read_text(encoding="utf-8")
    assert "AccountID,ProductType,EngineOrigin" in text
    assert DEFAULT_ACCOUNT_SB in text
    assert ENGINE_ORIGIN_SB in text
    assert "DIAAAAUPS002" in text


def test_engine_position_caps_from_config(monkeypatch) -> None:
    cfg = {
        "max_open_positions": None,
        "engine_position_caps": {
            ENGINE_CFD_SNIPER: None,
            ENGINE_SB_SENTINEL: 10,
        },
    }
    assert global_max_open_positions(cfg) is None
    assert engine_position_cap(ENGINE_CFD_SNIPER, cfg) is None
    assert engine_position_cap(ENGINE_SB_SENTINEL, cfg) == 10
