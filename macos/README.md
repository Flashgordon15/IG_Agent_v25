# IG Agent v41 — macOS Native Supervisor Launcher

Self-cleaning, PID-safe, port-safe one-click launch for DEMO (default). No terminal required when using **IGAgent.app**.

## Quick start

```bash
# Build Swift supervisor + app bundle
./macos/install_igagent_app.sh

# Desktop shortcut
./macos/setup_desktop_shortcut.sh

# Or run from CLI (same pipeline)
./macos/launcher/launch_agent.sh
```

Double-click **IG Agent.app** (`macos/IGAgent.app` or Desktop alias).

## Supervisor pipeline

| Step | Component | Action |
|------|-----------|--------|
| 1 | `IGAgentSupervisor` (Swift) or `igagent_launcher.sh` | Orchestrate kill → start → verify → GUI |
| 2 | `agent_kill.sh` | `mark_manual_stop`, stop.sh, kill all agent/watchdog/pytest/feed-hub/vite PIDs, free :8080/:5173, clear locks/PIDs/caches |
| 3 | `agent_start.sh` | DEMO reset, **isolated pytest subprocess** (hang-safe), preflight, fresh `daemon_supervisor`, poll G5, stable health, **unified route warm-up**, Vite dev server last |
| 4 | `agent_verify.sh` | Poll `/api/health` + `/api/gui_status` (v31–v41 fields) with clean timeout |
| 5 | `agent_gui.sh` | Open dashboard in browser |

## Build Swift supervisor binary

Requires Xcode Command Line Tools (`xcode-select --install`).

```bash
./macos/supervisor/build_swift.sh
# → macos/launcher/IGAgentSupervisor
```

Manual compile:

```bash
swiftc -O -o macos/launcher/IGAgentSupervisor \
  macos/launcher/IGAgentSupervisor.swift \
  -framework Foundation -framework UserNotifications -framework AppKit
chmod +x macos/launcher/IGAgentSupervisor
```

## Package IGAgent.app bundle

```bash
./macos/install_igagent_app.sh
open macos/IGAgent.app
```

This script:

1. Compiles `IGAgentSupervisor.swift` (if `swiftc` available)
2. Creates `macos/IGAgent.app/Contents/MacOS/IGAgent` from the Swift binary
3. Symlinks launcher scripts into `Contents/Resources/Scripts/`
4. Falls back to a bash wrapper if Swift compile fails

App bundle layout:

```
macos/IGAgent.app/
  Contents/
    Info.plist
    MacOS/IGAgent                 ← IGAgentSupervisor (Swift) or bash wrapper
    Resources/Scripts/            ← symlinks to macos/launcher/*
```

## Hang-safe pytest gate

`agent_start.sh` runs pytest in a **separate subprocess**. When the summary line `N passed` appears, it allows a grace period then sends TERM/KILL if the process is stuck in teardown. Launch continues without blocking on pytest hang.

## Environment overrides

| Variable | Effect |
|----------|--------|
| `IG_AGENT_ROOT` | Project root (auto-detected from app bundle or cwd) |
| `APP_MODE` | `DEMO` (default), `LIVE`, `TESTBED` |
| `IG_API_PORT` | Default `8080` |
| `LAUNCHER_SKIP_TESTS=1` | Skip pytest gate |
| `LAUNCHER_SKIP_DEMO_RESET=1` | Skip DEMO cache/P&L reset |
| `LAUNCHER_TEST_TIMEOUT_SEC` | Pytest max wait (default 900) |
| `LAUNCHER_TEST_GRACE_SEC` | Grace after pass summary (default 45) |
| `LAUNCHER_VERIFY_TIMEOUT_SEC` | GUI verify timeout (default 300) |
| `LAUNCHER_SKIP_NPM_DEV=1` | Do not auto-start Vite |
| `LAUNCHER_SKIP_GUI_SERVER=1` | Skip Vite entirely in agent_start |

## Logs

| File | Content |
|------|---------|
| `logs/supervisor_swift.log` | Swift supervisor script output |
| `logs/igagent_launcher.log` | Shell supervisor run |
| `logs/agent_kill.log` | Kill/clean phase |
| `logs/agent_start.log` | Start + warm-up |
| `logs/agent_verify.log` | GUI verification |
| `logs/pytest_gate.log` | Isolated pytest output |

## Optional Go binary

```bash
./macos/supervisor/build.sh   # requires Go toolchain
```

Priority: `IGAgentSupervisor` (Swift) → `igagent_launcher` (Go) → `igagent_launcher.sh` (bash).

## Safety

Launcher scripts only orchestrate process lifecycle and verification. They do **not** modify execution logic, sizing, REST, feed-hub internals, or unified routing rules.
