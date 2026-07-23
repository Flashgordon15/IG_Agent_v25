"""Core B thin ML / setup conviction gate — fail-closed."""

from __future__ import annotations

from types import SimpleNamespace

from ml.core_b_entry_gate import core_b_ml_allows_entry


def test_core_b_allows_when_ml_disabled():
    cfg = {"USE_ML_SIGNAL": False}
    ok, reason = core_b_ml_allows_entry("IX.D.DOW.IFM.IP", "BUY", cfg=cfg)
    assert ok is True
    assert reason == "ml_disabled"


def test_core_b_setup_conviction_blocks_low_wr(monkeypatch):
    mem = SimpleNamespace(veto=False, trades=20, win_rate=0.30, reason="")

    monkeypatch.setattr(
        "ml.feed_quality.evaluate_feed_quality",
        lambda *a, **k: SimpleNamespace(veto=False, reason="ok"),
    )
    monkeypatch.setattr(
        "ml.setup_memory.evaluate_setup_memory",
        lambda *a, **k: mem,
    )

    cfg = {
        "USE_ML_SIGNAL": True,
        "ml_veto": {"min_probability": 0.52, "min_labelled_rows": 30},
    }
    ok, reason = core_b_ml_allows_entry("IX.D.DOW.IFM.IP", "BUY", cfg=cfg)
    assert ok is False
    assert "setup_conviction" in reason


def test_core_b_feed_exception_fail_closed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("feed_down")

    monkeypatch.setattr("ml.feed_quality.evaluate_feed_quality", _boom)
    cfg = {"USE_ML_SIGNAL": True}
    ok, reason = core_b_ml_allows_entry("IX.D.DOW.IFM.IP", "BUY", cfg=cfg)
    assert ok is False
    assert "fail_closed" in reason


def test_core_b_ok_without_model_features(monkeypatch):
    mem = SimpleNamespace(veto=False, trades=2, win_rate=0.50, reason="")

    monkeypatch.setattr(
        "ml.feed_quality.evaluate_feed_quality",
        lambda *a, **k: SimpleNamespace(veto=False, reason="ok"),
    )
    monkeypatch.setattr(
        "ml.setup_memory.evaluate_setup_memory",
        lambda *a, **k: mem,
    )

    class _Scorer:
        def is_trained(self):
            return True

        @property
        def feature_names(self):
            return ["rsi", "atr_ratio"]

        def predict(self, features):
            return 0.40

    monkeypatch.setattr("trading.ml_scorer.get_ml_scorer", lambda: _Scorer())

    cfg = {"USE_ML_SIGNAL": True, "ml_veto": {"min_probability": 0.52}}
    ok, reason = core_b_ml_allows_entry("IX.D.DOW.IFM.IP", "BUY", cfg=cfg, quote=None)
    assert ok is True
    assert reason == "core_b_ml_ok"


def test_core_b_model_conviction_with_features(monkeypatch):
    mem = SimpleNamespace(veto=False, trades=2, win_rate=0.50, reason="")

    monkeypatch.setattr(
        "ml.feed_quality.evaluate_feed_quality",
        lambda *a, **k: SimpleNamespace(veto=False, reason="ok"),
    )
    monkeypatch.setattr(
        "ml.setup_memory.evaluate_setup_memory",
        lambda *a, **k: mem,
    )

    class _Scorer:
        def is_trained(self):
            return True

        @property
        def feature_names(self):
            return ["rsi", "atr_ratio"]

        def predict(self, features):
            return 0.40

    monkeypatch.setattr("trading.ml_scorer.get_ml_scorer", lambda: _Scorer())

    quote = SimpleNamespace(rsi=55.0, atr_ratio=1.1)
    cfg = {"USE_ML_SIGNAL": True, "ml_veto": {"min_probability": 0.52}}
    ok, reason = core_b_ml_allows_entry(
        "IX.D.DOW.IFM.IP", "BUY", cfg=cfg, quote=quote
    )
    assert ok is False
    assert "model_conviction" in reason
