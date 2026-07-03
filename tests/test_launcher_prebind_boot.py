"""Launcher pre-bind boot hardening — env forcing and lazy correlation_guard."""

from __future__ import annotations

import importlib
import sys
from unittest.mock import patch

import pytest


def test_launcher_forces_non_blocking_boot_after_dotenv_override(monkeypatch):
    """dotenv must not re-enable blocking boot on macOS launcher paths."""
    monkeypatch.setenv("IG_AGENT_FROM_LAUNCHER", "1")
    monkeypatch.setenv("IG_NON_BLOCKING_BOOT", "0")

    def _fake_dotenv(*_a, **_k):
        import os

        os.environ["IG_NON_BLOCKING_BOOT"] = "0"

    with patch("system.env_loader.load_dotenv", side_effect=_fake_dotenv):
        import main

        importlib.reload(main)
        main._force_launcher_non_blocking_boot()

    assert __import__("os").environ.get("IG_NON_BLOCKING_BOOT") == "1"


def test_correlation_guard_does_not_load_state_at_import(monkeypatch, tmp_path):
    """Importing correlation_guard must not touch disk until first use."""
    state_file = tmp_path / "state" / "correlation_guard.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        '{"buy": 9, "sell": 8, "buy_risk_gbp": 0, "sell_risk_gbp": 0, "session": "x"}',
        encoding="utf-8",
    )

    mod_name = "execution.correlation_guard"
    sys.modules.pop(mod_name, None)

    with patch("execution.correlation_guard._STATE_FILE", state_file):
        cg = importlib.import_module(mod_name)
        assert cg._state_loaded is False
        assert cg._buy_count == 0

        snap = cg.snapshot()
        assert snap["buy"] == 9
        assert cg._state_loaded is True


def test_boot_milestone_ordering_in_main_helpers():
    import main

    importlib.reload(main)
    main._boot_milestone_t0 = 0.0
    messages: list[str] = []

    with patch.object(main, "_log_engine", side_effect=lambda m: messages.append(m)):
        main._boot_milestone("alpha")
        main._boot_milestone("beta")

    assert messages[0].startswith("boot_milestone: alpha +")
    assert messages[1].startswith("boot_milestone: beta +")
    assert int(messages[1].split("+")[1].rstrip("ms")) >= int(
        messages[0].split("+")[1].rstrip("ms")
    )
