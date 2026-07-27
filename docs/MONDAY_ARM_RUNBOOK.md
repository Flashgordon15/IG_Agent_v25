# Monday Dual-Arm Runbook

One-page cash-open procedure for CFD `:8080` (QUANT_SNIPER) + SB `:8081` (MACRO_SENTINEL).  
**Never** `kill -9` main. Twins must be FLAT before arm.

## Sequence

```bash
# Tonight / pre-open (safe — no changes)
./scripts/monday_dual_arm.sh preflight
./scripts/monday_dual_arm.sh dry-run

# At cash open (London), after both books FLAT and outside rollover 21:58–22:05 BST
./scripts/monday_dual_arm.sh arm
```

`arm` refuses weekends / rollover / non-FLAT books. It stamps `operator_reopen_witness.json`, clears entry holds + bleed locks, lifts A2 CFD hard-block, then `POST /api/start` on **both** ports.

## Posture (option C)

| Engine | Port | Account | Role |
|--------|------|---------|------|
| CFD sniper | `:8080` | Z6BAH4 | Dual-armed; Instant path via QUANT_SNIPER |
| SB sentinel | `:8081` | Z6BAH3 | Dual-armed; **Instant/micro stay HARD OFF** — macro/LTR only |

## First 30 min watch

```bash
curl -s http://127.0.0.1:8080/api/health
curl -s http://127.0.0.1:8080/api/trade_support/status
curl -s http://127.0.0.1:8080/api/positions/live   # trust verdict / critical / broker_open_sot
curl -s http://127.0.0.1:8081/api/positions/live
curl -s http://127.0.0.1:8080/api/rotation_state
```

## Kill switches (either port)

```bash
./scripts/desk_dev_pause.sh pause                 # freeze NEW entries; keep supervision
curl -s -X POST http://127.0.0.1:8080/api/stop    # pause CFD
curl -s -X POST http://127.0.0.1:8081/api/stop    # pause SB
# Re-arm A2 CFD hard-block if needed (see monday_dual_arm.sh dry-run footer)
# Full teardown: ./scripts/desk_deploy.sh audit  (anti-zombie — never kill -9)
```

Supervisor auto-locks on bleed / APP_BLOCKED review — do not auto-resume past those.

## Session checkpoints

| After | Action |
|-------|--------|
| **8** stamped closes | `PYTHONPATH=src .venv/bin/python3 scripts/ml_strategy_review.py --day $DAY` |
| **20** stamped closes | Same + check APP share of losers + calibration / score lift |

Day = London calendar day (`YYYY-MM-DD`).

## Success gates (day 1)

- APP share of losers **&lt; 25%**
- HoldSec stamp rate **≥ 40%**
- `ml_strategy_review` leaves `NOT_MEASURABLE` (needs stamped sample)

## End-of-day cadence

```bash
./scripts/run_daily_loss_autopsy.sh $DAY --with-review --with-shadow
```

Optional weekday automation (read-only reports only):

```bash
./scripts/install_daily_loss_loop.sh          # install + load LaunchAgent @ 21:40 London
./scripts/install_daily_loss_loop.sh --unload # remove
```

## Risk caps (truth)

Global `max_open_positions: null` is **intentional** (dual-engine). Real caps:

- `engine_position_caps.cfd_sniper: 1`
- `engine_position_caps.sb_sentinel: 10`
- `position_management.enforce_cap_breach: true`

Health showing `max_open_positions: null` is correct — not an unenforced APP hole.
