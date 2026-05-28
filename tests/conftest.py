"""Pytest bootstrap — src on path; isolate engine log from live agent."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def isolate_engine_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep WAIT/signal lines from tests out of the dashboard engine.log."""
    monkeypatch.setenv("IG_AGENT_PYTEST", "1")
    monkeypatch.setattr("system.engine_log._LOG", tmp_path / "engine.log")
