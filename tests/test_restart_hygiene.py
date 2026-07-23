"""Restart hygiene — manual stop, supervisor dedup, clean slate."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from system.shutdown_cleanup import (
    clear_manual_stop,
    manual_stop_active,
    mark_manual_stop,
    stop_daemon_supervisor_processes,
)


@pytest.fixture(autouse=True)
def _clean_manual_stop(tmp_path: Path, monkeypatch):
    primary = tmp_path / "primary" / "state" / "manual_stop.json"
    legacy = tmp_path / "legacy" / "state" / "manual_stop.json"
    monkeypatch.setattr(
        "system.shutdown_cleanup._manual_stop_paths",
        lambda: (primary, legacy),
    )
    clear_manual_stop()
    yield
    clear_manual_stop()


def test_manual_stop_blocks_restart_until_cleared():
    mark_manual_stop(source="test")
    assert manual_stop_active() is True
    clear_manual_stop()
    assert manual_stop_active() is False


def test_manual_stop_writes_both_paths(tmp_path: Path, monkeypatch):
    primary = tmp_path / "primary" / "state" / "manual_stop.json"
    legacy = tmp_path / "legacy" / "state" / "manual_stop.json"
    monkeypatch.setattr(
        "system.shutdown_cleanup._manual_stop_paths",
        lambda: (primary, legacy),
    )
    mark_manual_stop(source="dual_path_test")
    assert primary.is_file()
    assert legacy.is_file()
    assert "dual_path_test" in primary.read_text(encoding="utf-8")
    assert "dual_path_test" in legacy.read_text(encoding="utf-8")


def test_manual_stop_active_if_either_path_present(tmp_path: Path, monkeypatch):
    primary = tmp_path / "primary" / "state" / "manual_stop.json"
    legacy = tmp_path / "legacy" / "state" / "manual_stop.json"
    monkeypatch.setattr(
        "system.shutdown_cleanup._manual_stop_paths",
        lambda: (primary, legacy),
    )
    clear_manual_stop()
    assert manual_stop_active() is False

    now = time.time()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"ts": now, "source": "legacy_only"}),
        encoding="utf-8",
    )
    assert manual_stop_active() is True

    clear_manual_stop()
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text(
        json.dumps({"ts": now, "source": "primary_only"}),
        encoding="utf-8",
    )
    assert manual_stop_active() is True


def test_clear_manual_stop_removes_both_paths(tmp_path: Path, monkeypatch):
    primary = tmp_path / "primary" / "state" / "manual_stop.json"
    legacy = tmp_path / "legacy" / "state" / "manual_stop.json"
    monkeypatch.setattr(
        "system.shutdown_cleanup._manual_stop_paths",
        lambda: (primary, legacy),
    )
    mark_manual_stop(source="clear_both")
    clear_manual_stop()
    assert not primary.exists()
    assert not legacy.exists()
    assert manual_stop_active() is False


def test_stop_daemon_supervisor_noop_under_pytest():
    assert stop_daemon_supervisor_processes() == []


def test_agent_kill_script_mark_before_kill_order():
    text = Path("macos/launcher/agent_kill.sh").read_text(encoding="utf-8")
    mark_pos = text.index("mark_manual_stop")
    term_pos = text.index("TERM — IG Agent process families")
    pre_purge = "pre-purge port"
    assert mark_pos < term_pos
    assert pre_purge not in text


def test_daemon_supervisor_respects_manual_stop():
    text = Path("scripts/daemon_supervisor.sh").read_text(encoding="utf-8")
    assert "manual_stop_engaged" in text
    assert "recovery suppressed — manual_stop active" in text
    assert "manual_stop hold (no auto-restart)" in text


def test_daemon_supervisor_kills_all_main_py_on_recovery():
    text = Path("scripts/daemon_supervisor.sh").read_text(encoding="utf-8")
    assert "SIGTERM all main.py" in text
    assert "evict_stale_processes full" in text
    assert "evict_stale_processes soft" not in text


def test_agent_start_clears_manual_stop_before_supervisor_spawn():
    text = Path("macos/launcher/agent_start.sh").read_text(encoding="utf-8")
    boot_anchor = text.index('log "[AGENT] fresh interpreter via daemon_supervisor"')
    boot_section = text[boot_anchor:]
    clear_pos = boot_section.index("clear_manual_stop")
    supervisor_pos = boot_section.index('"${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh"')
    assert clear_pos < supervisor_pos
    assert "DAEMON_SUPERVISOR_REDIRECT=1" not in boot_section
    preflight_end = text.index('log "[TEST]"')
    assert text[:preflight_end].count("clear_manual_stop") == 0


def test_detach_exec_helper_uses_perl_setsid_and_disown():
    text = Path("scripts/lib/detach_exec.sh").read_text(encoding="utf-8")
    assert "use POSIX qw(setsid)" in text
    assert "disown" in text
    assert "detach_exec" in text


def test_launch_scripts_source_detach_exec_helper():
    for rel in (
        "scripts/daemon_supervisor.sh",
        "scripts/start_agent_background.sh",
        "scripts/watchdog.sh",
        "scripts/v32_runtime_start.sh",
        "scripts/trading_desk_silent.sh",
    ):
        body = Path(rel).read_text(encoding="utf-8")
        assert "detach_exec.sh" in body, rel
        assert "detach_exec" in body, rel


def test_watchdog_defers_to_daemon_supervisor():
    text = Path("scripts/watchdog.sh").read_text(encoding="utf-8")
    assert "supervisor_managed" in text
    assert "daemon_supervisor active" in text
    assert "supervisor.pid" in text
    assert "daemon_supervisor booting" in text


def test_watchdog_checks_dual_manual_stop_paths():
    text = Path("scripts/watchdog.sh").read_text(encoding="utf-8")
    assert "v31-production/state/manual_stop.json" in text
    assert "src/data/state/manual_stop.json" in text
    assert "from system.shutdown_cleanup import manual_stop_active" in text


def test_startup_hold_clear_clears_cap_breach_when_flat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    cfd = tmp_path / "state_cfd"
    cfd.mkdir(parents=True)
    (cfd / "trading_paused.json").write_text(
        json.dumps(
            {"active": True, "reason": "stability_harness_cap_breach", "ts": time.time()}
        ),
        encoding="utf-8",
    )

    def _flat(_port: int = 8080, *, timeout: float = 2.0):
        return True

    monkeypatch.setattr(
        "system.startup_hold_clear.book_flat_via_api",
        _flat,
    )
    from system.startup_hold_clear import clear_stale_entry_holds_if_flat

    result = clear_stale_entry_holds_if_flat(reason="test")
    assert "state_cfd/trading_paused.json" in result["cleared"]
    raw = json.loads((cfd / "trading_paused.json").read_text(encoding="utf-8"))
    assert raw.get("active") is False


def test_startup_hold_clear_skips_when_book_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    cfd = tmp_path / "state_cfd"
    cfd.mkdir(parents=True)
    (cfd / "trading_paused.json").write_text(
        json.dumps({"active": True, "reason": "stability_harness_cap_breach"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "system.startup_hold_clear.book_flat_via_api",
        lambda *_a, **_k: False,
    )
    from system.startup_hold_clear import clear_stale_entry_holds_if_flat

    result = clear_stale_entry_holds_if_flat(reason="test")
    assert result["skipped"] == "book_not_flat"
    raw = json.loads((cfd / "trading_paused.json").read_text(encoding="utf-8"))
    assert raw.get("active") is True


def test_v32_runtime_start_evicts_trade_support_cache():
    text = Path("scripts/v32_runtime_start.sh").read_text(encoding="utf-8")
    assert "_evict_stale_trade_support_cache" in text
    assert "trade_support_status.json" in text
    assert "trade_support_sot." in text


def test_v32_runtime_start_defaults_skip_dual_launchd():
    text = Path("scripts/v32_runtime_start.sh").read_text(encoding="utf-8")
    assert 'IG_V32_SKIP_DUAL_LAUNCHD="${IG_V32_SKIP_DUAL_LAUNCHD:-1}"' in text
    assert "startup_hold_clear" in text


def test_trading_desk_silent_clears_stale_holds_before_cold_start():
    text = Path("scripts/trading_desk_silent.sh").read_text(encoding="utf-8")
    assert "startup_hold_clear" in text
    assert "detach_exec" in text


def test_book_flat_via_api_rejects_hollow_sot(monkeypatch) -> None:
    """False FLAT + empty rows must not clear holds when broker_open_sot > 0."""
    import io
    import json
    from urllib import error as urllib_error

    payload = {
        "verdict": "FLAT",
        "count": 0,
        "broker_open_sot": {"count": 12, "source": "trade_support"},
        "trade_support": {"broker_open": 12},
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "system.startup_hold_clear.urllib.request.urlopen",
        lambda *a, **k: _Resp(),
    )
    from system.startup_hold_clear import book_flat_via_api

    assert book_flat_via_api(8080) is False


def test_book_flat_via_api_true_when_truly_flat(monkeypatch) -> None:
    import json

    payload = {
        "verdict": "FLAT",
        "count": 0,
        "broker_open_sot": {"count": 0, "source": "flat"},
        "trade_support": {"broker_open": 0},
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "system.startup_hold_clear.urllib.request.urlopen",
        lambda *a, **k: _Resp(),
    )
    from system.startup_hold_clear import book_flat_via_api

    assert book_flat_via_api(8080) is True

