from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "fetch_historical_ohlc.py"
)
SPEC = importlib.util.spec_from_file_location("fetch_historical_ohlc", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _Rest:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def _auth_headers(self, _: str) -> dict:
        return {}

    def request(self, *_args, **_kwargs):
        self.calls += 1
        return self._responses.pop(0)


def test_append_bars_dedupes_and_orders(tmp_path: Path) -> None:
    out = tmp_path / "nikkei_5m.jsonl"
    seen: set[str] = {"2026-05-28T08:00:00"}
    bars = [
        {"t": "2026-05-28T08:10:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "spread": 1},
        {"t": "2026-05-28T08:05:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "spread": 1},
        {"t": "2026-05-28T08:05:00", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2, "spread": 2},
        {"t": "2026-05-28T07:55:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "spread": 1},
    ]
    added, last_written = fetch._append_bars(
        out, bars, seen, last_written="2026-05-28T08:00:00"
    )
    assert added == 2
    assert last_written == "2026-05-28T08:10:00"
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    assert '"t":"2026-05-28T08:05:00"' in lines[0]
    assert '"t":"2026-05-28T08:10:00"' in lines[1]


def test_fetch_page_allowance_block_returns_retry_metadata(monkeypatch) -> None:
    monkeypatch.setattr(fetch, "_next_retry_iso", lambda _mins: "2026-05-28T10:00:00")
    rest = _Rest(
        [
            _Resp(
                403,
                payload={
                    "errorCode": "error.public-api.exceeded-account-historical-data-allowance"
                },
            )
        ]
    )
    prices, page, blocked = fetch._fetch_page(
        rest,
        epic=fetch.EPIC,
        page_number=1,
        date_from="2026-05-01T00:00:00",
        date_to="2026-05-02T00:00:00",
    )
    assert prices == []
    assert page == {}
    assert blocked is not None
    assert blocked["block_reason"] == "allowance"
    assert blocked["next_retry_time"] == "2026-05-28T10:00:00"


def test_fetch_page_rate_limit_retries_then_blocks(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(float(s)))
    monkeypatch.setattr(fetch, "_next_retry_iso", lambda _mins: "2026-05-28T11:00:00")
    rest = _Rest(
        [
            _Resp(429, payload={"errorCode": "too_many_requests"}),
            _Resp(429, payload={"errorCode": "too_many_requests"}),
            _Resp(429, payload={"errorCode": "too_many_requests"}),
            _Resp(429, payload={"errorCode": "too_many_requests"}),
        ]
    )
    prices, page, blocked = fetch._fetch_page(
        rest,
        epic=fetch.EPIC,
        page_number=1,
        date_from="2026-05-01T00:00:00",
        date_to="2026-05-02T00:00:00",
    )
    assert prices == []
    assert page == {}
    assert blocked is not None
    assert blocked["block_reason"] == "rate_limit"
    assert blocked["next_retry_time"] == "2026-05-28T11:00:00"
    assert rest.calls == fetch.MAX_FETCH_RETRIES
    assert len(slept) == fetch.MAX_FETCH_RETRIES - 1
