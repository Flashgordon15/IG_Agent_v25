# IG Agent v25 — macOS desktop launcher

## Build app + Desktop icon

From project root:

```bash
python3 launcher/build_mac_app.py
```

This creates `launcher/IG Agent v25.app` and a symlink on your Desktop:
`~/Desktop/IG Agent v25.app`.

Double-click starts `src/main.py` (trading loop + dashboard on http://localhost:8080).

## What the launcher does

- Resolves project root from the app bundle location
- Refuses start if `emergency_stop.lock` exists
- Clears stale instance lock / duplicate `main.py` processes
- Uses Python 3.14 from the spec path when available
- Logs to `src/data/logs/launcher.log`

## Login at launch (optional)

Install launchd jobs (auto-start on login):

```bash
./scripts/install_launchd.sh
```
