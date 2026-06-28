# IG Agent v31 — macOS One-Click Launcher

Replace manual stop/clean/start steps with a single macOS app that runs the full v31 startup contract.

## Quick start

```bash
# One-time: Desktop shortcut
./macos/setup_desktop_shortcut.sh

# Or run directly
./macos/launcher/launch_agent.sh
```

Double-click **IG Agent v31** on the Desktop (symlink to `macos/IGAgentLauncher.app`).

## Startup contract (DEMO)

The launcher performs these phases in order:

| Phase | Action |
|-------|--------|
| **STOP** | `mark_manual_stop` → `./scripts/stop.sh --mode DEMO` → TERM/KILL if :8080 still bound |
| **CLEAN** | Purge `__pycache__` / `*.pyc`, remove stale lock files |
| **RESET** | DEMO only: strategy cache reset, daily P&L baseline refresh (not SQLite history) |
| **START** | `./scripts/start.sh --mode DEMO` (full pytest gate + supervisor) |
| **VERIFY** | Poll `/api/health` (G5) and `/api/gui_status` (all strategy fields) |
| **GUI** | Open `http://127.0.0.1:8080/` (starts `npm run dev` if `dashboard/dist` missing) |

Logs: `logs/launcher.log`

## macOS notifications

Uses `osascript` for progress notifications and critical alerts on failure.

## Environment overrides

| Variable | Effect |
|----------|--------|
| `LAUNCHER_SKIP_DEMO_RESET=1` | Skip DEMO P&L/cache reset |
| `LAUNCHER_SKIP_NPM_DEV=1` | Do not auto-start Vite dev server |
| `APP_MODE` | Default `DEMO` |

## App bundle layout

```
macos/
  IGAgentLauncher.app/
    Contents/
      Info.plist
      MacOS/IGAgentLauncher      # app entry → launch_agent.sh
      Resources/                 # icon placeholder
  launcher/
    launch_agent.sh              # main orchestrator
    launcher_core.py             # testable Python helpers
    lib_notify.sh                # osascript notifications
  setup_desktop_shortcut.sh
```

## Safety

- Idempotent: safe to run when agent is already stopped or hung
- Does **not** modify trading logic, execution, REST, or strategy behaviour
- Uses the same anti-zombie sequence as `.cursorrules` (manual stop before kill)
- LIVE mode is **not** exposed in the one-click app (DEMO only)

## Tests

```bash
PYTHONPATH=src pytest tests/test_launcher.py -p no:anyio -v
```

## Custom icon

Replace `Contents/Resources/icon.icns.placeholder` with a real `.icns` and add `CFBundleIconFile` to `Info.plist`.
