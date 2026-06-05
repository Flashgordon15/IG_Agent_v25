"""
End-to-end pipeline: seeded OHLC -> EnvironmentScorer -> SignalEngine.evaluate -> shadow log.

Real components only; shadow log path patched to a temp file for isolation.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.config_loader import ConfigLoader
from trading.environment_scorer import GATE_PASS_MIN, SAFE_DEFAULT_SCORE, EnvironmentScorer

ROOT = Path(__file__).resolve().parents[1]
EPIC = "IX.D.NIKKEI.IFM.IP"
MARKET = "Japan 225"
N_BARS = 100
SPREAD = 7.0


def _synthetic_quotes(n: int = N_BARS, *, seed: int = 42) -> list[Quote]:
    """Realistic random walk from 38000 with small 5-minute steps."""
    rng = random.Random(seed)
    mid = 38000.0
    end = datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(minutes=5 * (n - 1))
    quotes: list[Quote] = []
    for i in range(n):
        mid += rng.uniform(-12.0, 12.0)
        half = SPREAD / 2.0
        t = start + timedelta(minutes=5 * i)
        quotes.append(Quote(t, mid - half, mid + half))
    return quotes


def _read_shadow_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


@pytest.fixture
def shadow_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_path = tmp_path / "shadow_log.jsonl"

    def _tmp_data_dir() -> Path:
        tmp_path.mkdir(parents=True, exist_ok=True)
        return tmp_path

    monkeypatch.setattr("signals.signal_engine.data_dir", _tmp_data_dir)
    return log_path


def test_full_signal_to_shadow_log_pipeline(shadow_log_path: Path) -> None:
    cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
    quotes = _synthetic_quotes()

    engine = SignalEngine(cfg)
    engine.seed_ohlc_history(MARKET, quotes, aliases=[EPIC])
    assert engine.ohlc_seed_count(MARKET) == N_BARS

    scorer = EnvironmentScorer(engine, config=cfg, epic=EPIC, normal_spread=SPREAD)
    engine._environment_scorer = scorer
    scorer.on_ohlc_bootstrapped(MARKET)

    fitness_score = scorer.score(MARKET)
    assert isinstance(fitness_score, float)
    assert 0.0 <= fitness_score <= 100.0
    # Score may equal GATE_PASS_MIN when cold-start cap applies; just verify scorer ran
    assert not scorer.last_score().capped_cold_start or fitness_score == GATE_PASS_MIN

    before = _read_shadow_rows(shadow_log_path)
    result = engine.evaluate(MARKET)
    assert result is not None

    rows = _read_shadow_rows(shadow_log_path)
    assert len(rows) == len(before) + 1

    record = rows[-1]
    today = date.today().isoformat()
    assert str(record.get("timestamp", "")).startswith(today)

    required = (
        "timestamp",
        "market",
        "confidence",
        "rsi",
        "atr",
        "fitness",
        "would_have_fired",
        "gate_blocked_at",
    )
    for field in required:
        assert field in record, f"missing shadow log field: {field}"

    assert record["market"] == MARKET
    assert isinstance(record["confidence"], (int, float))
    assert isinstance(record["rsi"], (int, float))
    assert isinstance(record["atr"], (int, float))
    assert isinstance(record["fitness"], (int, float))
    assert isinstance(record["would_have_fired"], bool)
    assert record["gate_blocked_at"] is None or isinstance(record["gate_blocked_at"], str)
