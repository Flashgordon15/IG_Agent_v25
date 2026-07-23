"""Tests for shared broker snapshot + cross-process REST budget coordination."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import runtime.broker_snapshot as bs  # noqa: E402
import system.shared_rest_budget as srb  # noqa: E402


# --------------------------------------------------------------------------
# broker_snapshot
# --------------------------------------------------------------------------

def _patch_snapshot_root(monkeypatch, tmp_path: Path) -> Path:
    """Isolate snapshot I/O to tmp — primary only (no legacy/prod mirror bleed)."""
    primary = tmp_path / "broker_snapshot.json"

    def _paths() -> list[Path]:
        return [primary]

    monkeypatch.setattr(bs, "snapshot_path", lambda: primary)
    monkeypatch.setattr(bs, "_mirror_paths", _paths)
    monkeypatch.setattr(bs, "_write_paths", _paths)
    return primary


def test_write_read_roundtrip(tmp_path, monkeypatch) -> None:
    _patch_snapshot_root(monkeypatch, tmp_path)
    positions = [
        {"deal_id": "D1", "epic": "IX.D.DOW.IFM.IP", "direction": "SELL",
         "size": 0.5, "entry": 100.0, "pnl_gbp": 5.0},
    ]
    assert bs.write_snapshot(source="test", positions=positions) is True
    snap = bs.read_snapshot(max_age_sec=None)
    assert snap is not None
    assert snap["count"] == 1
    assert snap["source"] == "test"
    assert snap["positions"][0]["deal_id"] == "D1"
    assert "age_sec" in snap


def test_read_stale_returns_none(tmp_path, monkeypatch) -> None:
    primary = _patch_snapshot_root(monkeypatch, tmp_path)
    bs.write_snapshot(source="test", positions=[])
    # Force an old timestamp.
    import json
    data = json.loads(primary.read_text())
    data["ts"] = time.time() - 120
    primary.write_text(json.dumps(data))
    assert bs.read_snapshot(max_age_sec=10.0) is None
    assert bs.read_snapshot(max_age_sec=None) is not None  # age-agnostic still works


def test_read_missing_returns_none(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(bs, "snapshot_path", lambda: missing)
    monkeypatch.setattr(bs, "_mirror_paths", lambda: [missing])
    assert bs.read_snapshot(max_age_sec=None) is None
    assert bs.is_fresh() is False


def test_read_picks_freshest_mirror(tmp_path, monkeypatch) -> None:
    import json

    older = tmp_path / "older.json"
    newer = tmp_path / "newer.json"
    older.write_text(
        json.dumps(
            {
                "ts": time.time() - 30,
                "source": "old",
                "positions": [
                    {"deal_id": "D1", "entry": 1.0, "epic": "X", "direction": "BUY", "size": 0.5}
                ],
            }
        )
    )
    newer.write_text(
        json.dumps(
            {
                "ts": time.time() - 1,
                "source": "new",
                "positions": [
                    {
                        "deal_id": "D1",
                        "entry": 52388.7,
                        "epic": "X",
                        "direction": "BUY",
                        "size": 0.5,
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(bs, "_mirror_paths", lambda: [older, newer])
    snap = bs.read_snapshot(max_age_sec=60.0)
    assert snap is not None
    assert snap["source"] == "new"
    assert snap["positions"][0]["entry"] == 52388.7


def test_normalize_from_ig_items(tmp_path, monkeypatch) -> None:
    _patch_snapshot_root(monkeypatch, tmp_path)
    items = [
        {
            "position": {
                "dealId": "DX",
                "size": 1.5,
                "level": 66800.0,
                "direction": "SELL",
                "stopLevel": 66900.0,
                "limitLevel": 66500.0,
                "createdDateUTC": "2026-07-08T06:00:00",
            },
            "market": {"epic": "IX.D.NIKKEI.IFM.IP", "bid": 66700.0, "offer": 66710.0},
        }
    ]
    assert bs.write_snapshot(source="rest", items=items) is True
    snap = bs.read_snapshot(max_age_sec=None)
    assert snap["count"] == 1
    row = snap["positions"][0]
    assert row["deal_id"] == "DX"
    assert row["epic"] == "IX.D.NIKKEI.IFM.IP"
    assert row["size"] == 1.5
    assert row["stop_level"] == 66900.0
    assert row["limit_level"] == 66500.0
    assert row["bid"] == 66700.0
    assert row["offer"] == 66710.0
    assert row["pnl_gbp"] is not None


def test_hollow_snapshot_enriched_from_hub_quote(tmp_path, monkeypatch) -> None:
    """Coalesced REST can persist opens without bid/offer — enrich → valued."""
    import json

    primary = _patch_snapshot_root(monkeypatch, tmp_path)

    def _fake_lookup(epic: str, *, entry: float | None = None):
        assert epic == "IX.D.DOW.IFM.IP"
        assert entry == 52210.5
        return {
            "bid": 52220.0,
            "offer": 52223.0,
            "mid": 52221.5,
            "source": "test_hub",
        }

    monkeypatch.setattr(bs, "lookup_mark_quote", _fake_lookup)

    # Simulate a prior hollow last_good write (no enrich at write time).
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "source": "open_position_manager",
                "pid": 1,
                "count": 1,
                "account_upl": None,
                "positions": [
                    {
                        "deal_id": "DIAAAA_HOLLOW",
                        "epic": "IX.D.DOW.IFM.IP",
                        "direction": "BUY",
                        "size": 0.5,
                        "entry": 52210.5,
                        "pnl_gbp": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    snap = bs.read_snapshot(max_age_sec=None)
    assert snap is not None
    assert snap.get("quotes_enriched") is True
    row = snap["positions"][0]
    assert row["bid"] == 52220.0
    assert row["offer"] == 52223.0
    assert row["pnl_gbp"] is not None
    assert float(row["pnl_gbp"]) > 0  # BUY marked above entry

    from execution.open_position_rules import rows_from_snapshot_positions

    class _Cfg:
        position_management = {}

    valued_rows = rows_from_snapshot_positions(
        [
            {
                "deal_id": "DIAAAA_HOLLOW",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "entry": 52210.5,
                "pnl_gbp": None,
            }
        ],
        _Cfg(),
    )
    assert len(valued_rows) == 1
    assert valued_rows[0].pnl_gbp is not None


def test_ig_items_from_hollow_snapshot_include_marks(tmp_path, monkeypatch) -> None:
    _patch_snapshot_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bs,
        "lookup_mark_quote",
        lambda epic, *, entry=None: {
            "bid": 52220.0,
            "offer": 52223.0,
            "mid": 52221.5,
            "source": "test_hub",
        },
    )
    bs.write_snapshot(
        source="coalesce",
        positions=[
            {
                "deal_id": "D1",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "entry": 52210.5,
                "pnl_gbp": None,
            }
        ],
    )
    items = bs.ig_items_from_snapshot(max_age_sec=None)
    assert len(items) == 1
    mkt = items[0]["market"]
    assert float(mkt["bid"]) == 52220.0
    assert float(mkt["offer"]) == 52223.0


# --------------------------------------------------------------------------
# shared_rest_budget
# --------------------------------------------------------------------------

def test_record_and_recent_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(srb, "_LEDGER_PATH", tmp_path / "rest_budget.json")
    assert srb.recent_count("ig_positions") == 0
    for _ in range(4):
        srb.record("ig_positions")
    assert srb.recent_count("ig_positions") == 4
    # Other buckets isolated.
    assert srb.recent_count("ig_ledger") == 0


def test_over_global_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(srb, "_LEDGER_PATH", tmp_path / "rest_budget.json")
    for _ in range(10):
        srb.record("ig_positions")
    assert srb.over_global_limit("ig_positions", 10.0) is True
    assert srb.over_global_limit("ig_positions", 20.0) is False
    # Uncoordinated bucket never limited.
    assert srb.over_global_limit("ig_orders", 1.0) is False


def test_prune_old_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(srb, "_LEDGER_PATH", tmp_path / "rest_budget.json")
    import json
    old = time.time() - 120
    (tmp_path / "rest_budget.json").write_text(
        json.dumps({"ig_ledger": [old, old, time.time()]})
    )
    # Only the fresh one counts within the 60s window.
    assert srb.recent_count("ig_ledger") == 1


def test_is_coordinated() -> None:
    assert srb.is_coordinated("ig_positions") is True
    assert srb.is_coordinated("ig_ledger") is True
    assert srb.is_coordinated("yahoo") is True
    assert srb.is_coordinated("ig_orders") is False
    assert srb.is_coordinated("ig_confirms") is False
