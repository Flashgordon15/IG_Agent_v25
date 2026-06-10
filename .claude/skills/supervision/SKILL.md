---
name: supervision
description: Check and repair launchd supervision drift — the agent's built-in operator layer. Use before overnight sessions, after Stop Agent, or when Safe to Leave fails.
disable-model-invocation: false
---

You are the agent's supervision operator (Cursor-equivalent). Run these checks, interpret results, and repair when safe.

## 1. Supervision drift check

```bash
PYTHONPATH=src python3 scripts/supervision_check.py --repair
```

Report every `ISSUE` and `WARN`. Common issues:

| Issue | Meaning | Fix |
|-------|---------|-----|
| `overnight_armed_but_launchd_watchdog_missing` | Safe to Leave armed but launchd unloaded | `./scripts/install_launchd.sh` or re-bootstrap |
| `agent_running_without_watchdog` | Agent up with no supervisor | `./scripts/install_launchd.sh` |
| `manual_stop_active_agent_down` | Dashboard Stop — watchdog won't restart for ~10 min | Expected after Stop; clear via new launch or wait TTL |
| `duplicate_main_py_processes` | Two agents fighting | Stop one; check Desktop + launchd conflict |

## 2. Overnight readiness

```bash
./scripts/ensure_overnight_ready.sh
```

Must pass before declaring Safe to Leave. Requires AC power.

## 3. Health API (when agent running)

```bash
curl -s http://127.0.0.1:8080/api/health | python3 -m json.tool
```

Check: `supervision_drift_ok`, `watchdog_active`, `overnight_supervision.launchd_watchdog`, `issues`.

## 4. Rules for AI operators

- **Stop Agent** must NOT unload launchd — supervision survives Stop.
- **install_launchd.sh** must NOT kill a healthy agent — handoff only.
- After code changes, Stop Agent then relaunch (or watchdog restart).
- Never delete `watchdog.pid` while launchd watchdog is loaded.
- Logs: `src/data/logs/watchdog.log`, `watchdog_launchd.log`, `engine.log` (search `supervision_monitor`).

If drift persists after `--repair`, inspect launchd logs and report specific failure — do not declare overnight-ready.
