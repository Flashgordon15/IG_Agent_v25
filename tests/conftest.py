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
    """Keep WAIT/signal lines from tests out of the dashboard engine.log.

    Also redirects runtime_state_persist writes to a per-test temp file so
    that running the test suite can never corrupt the live runtime_state.json.
    """
    monkeypatch.setenv("IG_AGENT_PYTEST", "1")
    monkeypatch.setattr("system.engine_log._LOG", tmp_path / "engine.log")

    import system.runtime_state_persist as rsp

    rsp.set_state_path_for_tests(tmp_path / "runtime_state.json")
    yield
    rsp.reset_persist_state_for_tests()
