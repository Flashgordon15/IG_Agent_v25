# IG Agent v25 — macOS desktop launcher

## Build app + Desktop icon

From project root:

```bash
python3 launcher/build_mac_app.py
```

This creates `launcher/IG Agent v25.app` and a symlink on your Desktop:
`~/Desktop/IG Agent v25.app` (symlink — shows the real app icon; not a Finder alias).

Double-click starts the agent in the background (no Terminal window) and opens
http://localhost:8080/ in your default browser when the API is healthy.

Equivalent command:

```bash
cd /path/to/IG_Agent_v25
PYTHONPATH=src python3 src/main.py
```

## What the launcher does

- Resolves project root from the app bundle location
- Refuses start if `emergency_stop.lock` exists
- Clears stale instance lock / duplicate `main.py` processes
- Uses Python 3.14 from the spec path when available
- Logs to `src/data/logs/launcher.log`

## Overnight / unattended trading

**Safe to Leave = overnight bundle.** Clicking it in the dashboard:

1. Ensures **launchd** watchdog + caffeinate are loaded (auto-bootstrap if plists exist)
2. Runs all trust checks (health, gates, quotes, AC power, telegram, …)
3. **Arms overnight mode** — you may close Cursor and the browser tab

The agent does **not** depend on Cursor. Use Cursor only for code changes.

One-time install:

```bash
./scripts/install_launchd.sh
```

Before bed (CLI equivalent of the dashboard button):

```bash
./scripts/ensure_overnight_ready.sh
```

**Stop Agent** clears overnight armed and stops launchd watchdog (no auto-restart for 10 min).
