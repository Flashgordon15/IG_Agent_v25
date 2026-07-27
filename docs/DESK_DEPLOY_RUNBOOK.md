# Desk Deploy Runbook

**Agreement:** Operator issues upgrades on disk → agent runs `scripts/desk_deploy.sh`.
**Dev-with-opens is always possible** — never treat a flat book as a hard prerequisite.

## Operator pause / offline / resume

| Goal | Command |
|------|---------|
| **Pause entries** (preferred hotfix; supervisors stay) | `./scripts/desk_dev_pause.sh pause` |
| **Resume entries** | `./scripts/desk_dev_pause.sh resume` |
| **Offline with opens** (main down, trade_support up) | `./scripts/desk_dev_offline.sh` |
| **Deploy with opens** | `./scripts/desk_deploy.sh deploy --force-open-book` (or `--dev`) |
| **Status** | `./scripts/desk_dev_pause.sh status` / `./scripts/desk_dev_offline.sh status` |

Pause freezes new entries via `entry_halt` + `trading_paused` + `deploy_hold`.
OPM + `trade_support` keep supervising opens. Never `kill -9`.

## Dual-port supervision (v32)

| Goal | Command |
|------|---------|
| **Arm dual watchdog** (no engine restart) | `./scripts/install_v32_dual_watchdog.sh` |
| **Status** | `./scripts/install_v32_dual_watchdog.sh status` |
| **Bootout dual only** | `./scripts/install_v32_dual_watchdog.sh bootout` |

- Label: `com.igagent.v32.dual` — `RunAtLoad` + `KeepAlive` (observer loop).
- Env `IG_V32_DUAL_PORT=1` → `watchdog.sh` watches `:8080`/`:8081` and **defers** single-engine restarts (heal via `v32_runtime_start.sh`).
- Default `v32_runtime_start.sh start` still skips bootstrap (`IG_V32_SKIP_DUAL_LAUNCHD=1`) so a twin recycle does not fight an existing desk; arm with the install script after start, or set `IG_V32_SKIP_DUAL_LAUNCHD=0` when you want start to bootstrap.
- Never re-enable legacy `com.igagent.v25.watchdog` while dual twins are live.

**Trade support:** one `runtime.trade_support_wrapper` (prefer `com.igagent.trade_support` KeepAlive). Dedup stale detached copies with `TERM` only after flat-book confirm.

## Boot-gate splash (confidence gate)

Every `Trading_Desk.app` / `trading_desk_silent.sh` launch opens
`http://127.0.0.1:3000/boot` first. The Quantum Terminal splash polls
`GET /api/desk/stability` until `boot_gate.ready_for_desk=true`, then reveals
the main desk.

| Field | Meaning |
|-------|---------|
| `ready_for_desk` | Path armed to **enter when signals fire** — not a fill guarantee |
| `checks[]` | pass / warn / fail / healing rows from the AI harness |
| `upgrades_live` | From `data_dir()/state/desk_upgrade_manifest.json` |
| Stuck >5m | Splash shows operator hints — **never fake green** |

```bash
# Operator one-shot
./scripts/desk_stability_status.sh
curl -s http://127.0.0.1:8080/api/desk/stability | python3 -m json.tool | head -80
```

Append an upgrade line after a deploy (optional):

```bash
PYTHONPATH=src python3 -c "
from runtime.desk_upgrade_manifest import append_upgrade
append_upgrade(upgrade_id='my_fix', title='Short title', detail='What landed')
"
```

## Session states

| State | Meaning | Deploy? |
|-------|---------|---------|
| **dev** | Agent down or manual_stop | Yes — use offline helper |
| **deploy_window** | Agent up, flat | Yes — preferred |
| **active_session** | `broker_open > 0` | Yes — auto `--force-open-book` + entry pause + inflight adopt |
| **manual_stop** | Watchdog hold after deliberate stop | Deploy after clearing hold / offline path |

Touch `src/data/state/deploy_hold.json` or set `desk_deploy.hold_active_session: true` to log boot warnings during active sessions (informational only).

## EDITS_ONLY close queue

Failed flattens with `EDITS_ONLY` / not-tradeable are persisted to
`data_dir()/edits_only_close_queue.json` and drained by `trade_support` (and OPM
on tick timeout) when the epic returns to `TRADEABLE`/`OPEN`.

```bash
# Inspect
python3 -c "from execution.edits_only_close_queue import load_queue; import json; print(json.dumps(load_queue(), indent=2))"
```

## Data root (unified)

Session `IG_DATA_ROOT` / `IG_AGENT_DATA_DIR` and `system.paths.data_dir()` must agree on:

`src/data/v31-production/`

Bridge legacy `src/data/` artifacts (learning DB symlink, trade_support status, broker_snapshot) before certify:

```bash
PYTHONPATH=src python3 scripts/unify_data_root.py --check
PYTHONPATH=src python3 scripts/unify_data_root.py --apply   # flat window preferred
```

## Pre-deploy audit checklist

Run **read-only** first:

```bash
./scripts/desk_deploy.sh audit
```

Confirm:

- [ ] **Flat** — `broker_open: 0` (or `--force-supervised` with supervise loop running)
- [ ] **manual_stop** — not active (or intentional teardown)
- [ ] **Market** — not in rollover lock `21:58–22:05 BST` if you care about entry timing post-restart
- [ ] **Three processes** — `main`, `trade_support`, `desk_support` identified
- [ ] **Wrappers fresh** — `trade_support_status.json` age &lt; 60s when positions open
- [ ] **OPM** — `tick_count > 0`, no perpetual `tick_in_progress`

## Anti-zombie protocol (exact sequence)

Never `kill -9` main.py without TERM first. Never kill main in isolation while launchd watchdog is active.

```bash
export APP_MODE=DEMO
export IG_AGENT_CONFIG=config/config_v31_demo_throughput.json
export PYTHONPATH=src

# A. Engage hold — freeze launchd watchdog
PYTHONPATH=src .venv/bin/python3 -c \
  "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='operator_restart')"

# B. Graceful shutdown — SIGTERM main, wait for port 8080 free
kill -TERM "$(pgrep -f 'src/main.py' | head -1)"
# wait up to 30s:
while lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; do sleep 1; done

# C. Evict cache — only after port is free
find src -type d -name __pycache__ -prune -exec rm -rf {} +
find src -name '*.pyc' -delete
rm -f src/data/.ig_agent_v29.lock src/data/.ig_agent_v31.lock
```

`desk_deploy.sh deploy` runs this sequence automatically, then starts via `session_ready.py --start-agent`.

## Three-process sync

| Process | Role | Restart |
|---------|------|---------|
| `src/main.py` | Agent + API + in-process OPM | `deploy` only |
| `runtime.trade_support_wrapper` | Broker-authoritative open-trade supervisor | After every main restart |
| `runtime.desk_support_wrapper` | Out-of-process health / recovery | Verify alive after main restart |

**Rule:** After every main restart → `sync-wrappers` (or full `deploy` which includes it).

```bash
./scripts/desk_deploy.sh sync-wrappers   # no main restart
```

## Deploy commands

```bash
./scripts/desk_deploy.sh audit              # read-only
./scripts/desk_deploy.sh certify            # smoke tests, no restart
./scripts/desk_deploy.sh deploy             # flat only — anti-zombie + all 3
./scripts/desk_deploy.sh deploy --force-supervised   # opens OK if supervise loop up
./scripts/desk_deploy.sh sync-wrappers      # wrappers only
```

Environment is always forced to `APP_MODE=DEMO` and `IG_AGENT_CONFIG=config/config_v31_demo_throughput.json`.

## Post-deploy smoke tests

`deploy` runs certify automatically. Manual:

```bash
./scripts/desk_deploy.sh certify
# or
PYTHONPATH=src python3 scripts/verify_session_live.py
```

Quick curls:

```bash
curl -s http://127.0.0.1:8080/api/health | python3 -m json.tool
curl -s http://127.0.0.1:8080/api/positions/live
curl -s http://127.0.0.1:8080/api/position_manager/status
curl -s http://127.0.0.1:8080/api/trade_support/status
```

Pass criteria: health `ok`, OPM `active`, rotation sweep &gt; 0, `unmonitored: 0` when positions exist.

## When NOT to deploy

- Broker has open positions (unless `--force-supervised` **and** `manage_live_positions.py --supervise-loop` running)
- Mid-session stacked upgrades not yet tested (`certify` fails offline)
- Rollover lock if you need immediate entries (optional operator preference)
- `manual_stop` not cleared and you expect watchdog auto-restart
- Partial wrapper restart only — always sync all three after main

## Rollback

1. **Hold watchdog:** `mark_manual_stop` (as in anti-zombie step A)
2. **Restore prior code** (git checkout / prior tree)
3. **Session boundary restart:**

```bash
PYTHONPATH=src python3 scripts/session_ready.py --start-agent
./scripts/desk_deploy.sh sync-wrappers
./scripts/desk_deploy.sh certify
```

4. If flat and corrupted state: `session_ready.py` (offline prep) → `deploy`

## Virtual vs broker stops (operator visibility)

Trading Desk uses a **three-layer software stack** on every open position:

| Layer | Module | What it does |
|-------|--------|--------------|
| GBP exit track | `micro_gbp_exit` | Soft loss (~£1.68), hard cap (£4), profit trail floor |
| Virtual stop | `virtual_stop_loss` | Point ceiling before broker sync |
| Dynamic trail | `dynamic_limit_engine` | Ratchets trail; may sync IG stop **after profit** |

**Default (current config):** `micro_risk.omit_broker_limit_at_entry: false` — entries attach a **broker stop** at open (EDITS_ONLY / weekend safety). Profit limits remain software-managed until dynamic trail syncs. Requires **flat deploy** for the config change to load into a running agent.

**Software stack still primary for profit exits:** OpenPositionManager (~6s) + `trade_support_wrapper` (broker-authoritative supervisor). Blank IG *limits* may still appear; blank *stops* are no longer the default.

**Critical alarm — never ignore:** if flatten REST fails (e.g. `EDITS_ONLY`), `/api/positions/live` returns `verdict: "CRITICAL"`, `critical_alarms[]`, and `trade_support.last_flatten_error`. Do **not** treat blank IG limits or Desk FLAT as safe in that state. Trust `trade_support.broker_open` + valued P&L over cache-only rows.

**Verify protection on desk:**

```bash
curl -s http://127.0.0.1:8080/api/positions/live | python3 -m json.tool
# Per position: protection_summary + flatten_failed / critical_alarm
# Quantum Terminal + Dashboard: G/V/D badges + Risk £ + CRITICAL banner
curl -s http://127.0.0.1:8080/api/trade_support/status | python3 -m json.tool
```

## Operator workflow

1. **You:** land upgrades on disk, run tests in blocks locally
2. **Agent:** `./scripts/desk_deploy.sh audit` — if `active_session`, stop at `sync-wrappers` only
3. **You:** flatten or wait for session end
4. **Agent:** `./scripts/desk_deploy.sh deploy` → prints `DEPLOY: PASS` or `FAIL`
5. **Trade** only after PASS
