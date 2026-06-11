"""External API key loader."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def test_finnhub_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from system import external_keys as ek

    ek.reset_external_keys_cache_for_tests()
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setenv("FINNHUB_API_KEY", "test-finnhub")
    ek.reset_external_keys_cache_for_tests()
    assert ek.finnhub_api_key() == "test-finnhub"


def test_keys_from_optional_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from system import external_keys as ek

    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "external_keys.json").write_text(
        json.dumps({"finnhub_api_key": "file-key", "alphavantage_api_key": "av-key"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ek, "project_root", lambda: tmp_path)
    ek.reset_external_keys_cache_for_tests()
    assert ek.finnhub_api_key() == "file-key"
    assert ek.alphavantage_api_key() == "av-key"
