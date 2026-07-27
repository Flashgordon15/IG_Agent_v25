"""v32 dual launchd plist must KeepAlive so dual watchdog actually supervises."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v32_dual_plist_keepalive_enabled():
    src = (ROOT / "scripts" / "com.igagent.v32.dual.plist").read_text(encoding="utf-8")
    assert "<string>com.igagent.v32.dual</string>" in src
    assert "<string>--dual-port</string>" in src
    assert "<key>RunAtLoad</key>" in src
    assert "<key>KeepAlive</key>" in src
    # Must not leave both false (loaded but never runs).
    assert "<false/>" not in src.split("<key>RunAtLoad</key>", 1)[1].split("<key>", 1)[0]
    keepalive_block = src.split("<key>KeepAlive</key>", 1)[1].split("<key>", 1)[0]
    assert "<true/>" in keepalive_block


def test_v32_generate_dual_plist_emits_keepalive():
    text = (ROOT / "scripts" / "v32_runtime_start.sh").read_text(encoding="utf-8")
    assert "generate_dual_plist()" in text
    # Generator body uses true for RunAtLoad/KeepAlive
    gen = text.split("generate_dual_plist()", 1)[1].split("launchctl_bootout_label", 1)[0]
    assert "<key>RunAtLoad</key>" in gen
    assert "<true/>" in gen
    assert "<key>KeepAlive</key>" in gen
    assert "ThrottleInterval" in gen


def test_watchdog_launchd_dual_port_flag_sets_env(monkeypatch, tmp_path):
    import scripts.watchdog_launchd as wl  # type: ignore

    # Import via path load — module is under scripts/ not package
    import importlib.util

    path = ROOT / "scripts" / "watchdog_launchd.py"
    spec = importlib.util.spec_from_file_location("watchdog_launchd", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    called: dict = {}

    def fake_call(cmd, cwd=None, env=None):
        called["cmd"] = cmd
        called["env"] = env
        return 0

    monkeypatch.setattr(mod.subprocess, "call", fake_call)
    # Ensure watchdog.sh exists relative to root
    assert (ROOT / "scripts" / "watchdog.sh").is_file()
    monkeypatch.setenv("IG_AGENT_ROOT", str(ROOT))
    rc = mod.main(["--dual-port"])
    assert rc == 0
    assert called["env"]["IG_V32_DUAL_PORT"] == "1"
    assert "8080,8081" in called["env"].get("IG_V32_WATCH_PORTS", "")


def test_install_v32_dual_watchdog_script_exists():
    script = ROOT / "scripts" / "install_v32_dual_watchdog.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "com.igagent.v32.dual" in text
    # Forbid actual SIGKILL kickstart invocation (comments may mention -k).
    assert "kickstart -k \"" not in text
    assert "kickstart -k '" not in text
    assert "com.igagent.v25.watchdog" in text  # must keep legacy unloaded
