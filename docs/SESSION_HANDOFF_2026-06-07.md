# Session handoff — 7 Jun 2026

Condensed notes from the v25.5.0 audit, merge, and shutdown session.

## Current state

| Item | Value |
|------|-------|
| **Branch** | `main` @ `3ae5aa1` (synced with `origin/main`) |
| **Version** | **25.5.0** |
| **Agent** | **Fully stopped** — safe to power off laptop |
| **Manual stop** | Flagged (`src/data/state/manual_stop.json`) — no auto-restart until icon click |
| **PR** | [#4](https://github.com/Flashgordon15/IG_Agent_v25/pull/4) merged 07:33 UTC |

## What shipped in v25.5.0

### Dashboard audit (P0–P2)
- Trades: dedupe IG imports, BREAKEVEN/PENDING labels, forex filter
- Header: "Today P&L", "Win (last 20)"
- Live: `fmtLogTs`, short market labels
- Points: 0.5× CAUTION band, decimals, tooltip
- System: REST /3 display, `fmtTs` fix
- Changelog splash once per version (`localStorage`)

### Lifecycle / overnight ops
- Startup + stop verification (`shutdown_cleanup.py`, `shutdown_verify_server.py`)
- Watchdog respects `manual_stop_active()`
- Launcher: `code_newer_than_agent` warning, notifications
- `install_launchd.sh`: `__PYTHON_BIN__` plist substitution
- Market-aware quote health (`agent_health.py`)

### Wiring fixes
- `routes.py`: import `run_safe_to_leave` (was 500 on Safe to Leave)
- `agent_bootstrap.py`: transaction sync singleton on bootstrap (reconcile was 503)
- `snapshot_store.py`: WebSocket ticks via `_tick_for_readers()`

## Verification done

- **Cold-start E2E** (7 Jun): 12/12 startup phases, 19/19 API audit, Safe to Leave 13/13, shutdown 5/5
- **Post-merge** (main): 56 tests pass; live agent reported splash **25.5.0**, Safe to Leave **13/13**
- **Sunday 23:00 BST**: Gold + Nikkei open; Wall St/Nasdaq ~Mon 01:00

## Backup

- `/Users/chrisgordon/Desktop/IG_Agent_v25_backups/IG_Agent_v25_full_backup_20260607_082507.tar.gz` (89 MB)
- Manifest: `BACKUP_MANIFEST_20260607_082507.txt`

## Shutdown (end of session)

1. `POST /api/shutdown` — graceful agent exit
2. Watchdog killed (does not exit loop on manual-stop alone)
3. `com.igagent.v25.caffeinate` launchd job unloaded
4. `confirm_stopped.py` → **5/5 PASS**

## Resume tomorrow

```bash
# Click desktop icon, or:
/Users/chrisgordon/Desktop/IG_Agent_v25/launcher/IG\ Agent\ v25.app/Contents/Resources/launch.sh
```

Clears manual-stop flag and cold-starts (~30–45 s). Fast path (~1 s) only if agent already healthy on `:8080`.

Optional pre-flight before overnight live session:

```bash
PYTHONPATH=src python3 scripts/pre_flight_check.py --live
PYTHONPATH=src python3 scripts/e2e_platform_validation.py
```

## Git notes

- Stash `runtime-state-verify` holds local edits to `config/config_v25.json` and `src/data/version.json` from branch switch — restore with `git stash pop` if needed
- Do **not** commit runtime files: `dashboard_snapshot.json`, `*.sqlite3-wal`, `watchdog.pid`, lock files
- ~80 Finder duplicate `* 2.*` files are junk — ignore

## Key commands

```bash
PYTHONPATH=src python3 src/main.py                    # run agent
PYTHONPATH=src python3 -m pytest tests/ -x -q         # tests
cd dashboard && npm run build                         # after dashboard edits
PYTHONPATH=src python3 scripts/confirm_stopped.py     # verify stopped
rm -f src/data/.ig_agent_v25.lock                     # stale lock after crash
```

## Chat reference

Full transcript: Cursor chat `d4dd3b8a-5c8f-4863-b35f-01416c05d441`
