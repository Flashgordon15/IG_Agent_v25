# GUI Desk Supervisor — Phase 1 + Phase 2

Permanent **observe + score + resolve queue** for the Trading Desk GUI path, plus **allowlisted self-heal only**.

| Allowed | Forbidden |
|---------|-----------|
| Curl health / positions / trade_support / accounting / stability / ops_strip / rotation / state | Raise REST 3/min hard cap |
| Score `PASS` \| `WATCH` \| `FAIL` | Re-enable Instant/micro / loosen ElasticGate / OBI fail-open |
| Write SoT JSON + MD + jsonl history + silence tracker | Strategy / alpha rewrites / `allow_non_dow` unlock |
| `cursor_handoff` when `needs_code` | `kill -9` / SIGKILL |
| Desk chip WATCH/FAIL under SYSTEM OPERATIONAL | Lifting A2 via `POST /api/start` on `:8080` |
| Phase-2 allowlisted heals (table below) | Heal after **2/hour** cap (hard FAIL + escalate) |

---

## Operator commands

```bash
# Enable (install LaunchAgent + first run)
./scripts/install_gui_desk_supervisor.sh --enable

# Disable
./scripts/install_gui_desk_supervisor.sh --disable

# Status + last score
./scripts/install_gui_desk_supervisor.sh --status

# Manual one-shot (observe + write SoT)
./scripts/install_gui_desk_supervisor.sh --run-once

# Heal dry-run (plan only)
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/gui_desk_supervisor.py --heal-dry-run

# Execute allowlisted heals (flat books required for recycle)
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/gui_desk_supervisor.py --heal

# G2G live verify
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/verify_gui_desk_supervisor_g2g.py --heal-dry-run
```

LaunchAgent label: `com.igagent.gui_desk_supervisor`  
Schedule: `StartInterval` **120s** + `RunAtLoad`  
Plist template: `scripts/com.igagent.gui_desk_supervisor.plist`

Default LaunchAgent run is **observe-only**. Set `IG_GUI_SUP_AUTO_HEAL=1` in the plist env only if you want dry-run planning every cycle; use explicit `--heal` for mutation.

---

## Source of truth (SoT)

| Path | Role |
|------|------|
| `src/data/v31-production/state/gui_supervisor_latest.json` | Machine-readable score + ranked findings + chip + heal |
| `src/data/v31-production/reports/gui_supervisor_latest.md` | Operator markdown |
| `src/data/v31-production/state/gui_supervisor_history.jsonl` | Append-only compact history |
| `src/data/v31-production/state/gui_supervisor_silence.json` | ARMED silence timer state |
| `src/data/v31-production/state/gui_supervisor_heal_log.jsonl` | Heal audit |
| `src/data/v31-production/state/gui_supervisor_heal_budget.json` | 2/hour heal cap window |

Code: `src/runtime/gui_desk_supervisor.py` + `src/runtime/gui_desk_supervisor_heal.py`  
CLI: `scripts/gui_desk_supervisor.py`  
Chip: `terminal/src/components/gpu/GuiSupervisorChip.tsx` (mounted under SYSTEM OPERATIONAL in `GpuPlatformShell`)  
API: `terminal/src/app/api/desk/gui_supervisor/route.ts`

---

## Phase 1 — what each run checks

1. `:8080` / `:8081` — health, positions/live, trade_support, liveness, stability, ops_strip, accounting, rotation_state, state
2. **Loops armed / `accepting_ticks`** (STUCK if READY/trade_ready but dormant; A2 pause on CFD is ignore)
3. **Path A carve vs MICRO block on SB** (`hard_allow`/`hard_block` when `sb_macro_ltr_entries_only`)
4. **Ranked promote** vs `exclude_from_hot_path` / `ranked_candidate_epics`
5. **A2 CFD pause** marker vs `trading_paused` on `:8080`
6. **Zero-attempt / silence timer** — SB ARMED (`accepting_ticks`, not paused) with no activity for `IG_GUI_SUP_SILENCE_MINUTES` (default **30**); held during vector warm
7. **ml_confidence warm** — WATCH if boot ready but confidence stuck 0; ignore while warming
8. **Cash merge** — near-equal dual-port today nets ⇒ shared journal single-count
9. **REST** pressure (CRITICAL=FAIL, ELEVATED=WATCH; never raise cap)
10. `:3000` Quantum Terminal reachability
11. Hung API: TCP LISTEN but health timeout

Findings ranked with `class`: `ops` | `ui` | `code` | `strategy` | `ignore`, plus `needs_code` / `needs_ops`.

When `needs_code` is true, JSON includes `cursor_handoff` (paste blurb + suspected files + allowed/forbidden).

Dashboard chip: silent on PASS; amber **WATCH** / red **FAIL** under the operational banner.

---

## Phase 2 — heal allowlist

| Action | Trigger | Behaviour | Flat books? |
|--------|---------|-----------|-------------|
| `port_hung_soft_recycle` | LISTEN + API timeout on CFD/SB | `mark_manual_stop` → **SIGTERM only** → clear locks → spawn that port; re-apply A2 `/api/stop` on CFD; `/api/start` SB if was armed | **Yes** (else escalate) |
| `loops_not_arming_unpause_or_recycle` | READY/trade_ready but `accepting_ticks=false` | `/api/start` unpause; if still stuck and flat → one soft recycle | Recycle yes / unpause no |
| `ui_restart_only` | `:3000` down | `launchctl kickstart com.igagent.v30.ui` and/or `start_ui_background.sh` | No |
| `armed_silence_soft_pause_sb` | Silence FAIL | Optional `POST :8081/api/stop` (halt bleed) — **does not loosen gates** | No |
| `reapply_a2_cfd_pause` | A2 marker active, CFD not paused | `POST :8080/api/stop` + stamp marker | No |

**Cap:** 2 heals / hour → hard FAIL + ops escalate.

**Never:** `kill -9`, raise REST, re-enable Instant/micro, loosen ElasticGate/OBI, strategy rewrites, `allow_non_dow` unlock, lift A2 with `/api/start` on CFD.

If books are not flat, recycle heals are dry-run/planned only and documented as **live cutover needed**.

---

## How Cursor reads the queue

1. Open `src/data/v31-production/state/gui_supervisor_latest.json` (or `.md` twin).
2. If `score` is `PASS` and both flags false → nothing to do.
3. If `needs_code` → use `cursor_handoff` + code-class findings.
4. If `needs_ops` → ops-class findings (launchd, pause re-engage, UI).
5. Always honour `a2.preserve` — keep CFD A2 pause unless the operator explicitly lifts it.
6. Inspect `heal.plans` / `heal.result` after `--heal-dry-run` or `--heal`.

---

## Scoring notes

- **FAIL** — unreachable/hung agent/UI, positions CRITICAL, cash double-count, A2 marker but CFD not paused, REST CRITICAL, STUCK loops, silence timer exceeded, heal cap exceeded.
- **WATCH** — REST ELEVATED, thin blotter, watchdog inactive, divergent dual cash, ml warm failure after ready, ranked config ON but inactive.
- **PASS** — path live, flat/healthy opens, A2 consistent, cash single-count, UI up, loops accepting on armed SB, ranked OK.

Expected soft signals under A2 (often **ignore**): health `ok:false` with only `trading_paused` / `watchdog_inactive`; ml_confidence=0 while vector warm.

---

## G2G gate

```bash
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_gui_desk_supervisor.py -q
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/verify_gui_desk_supervisor_g2g.py --heal-dry-run
# if terminal touched:
cd terminal && npx tsc --noEmit
```

G2G = pytest green **and** verify script exits 0 with `G2G=YES` (score PASS or WATCH-only, no unfixed STUCK_BUG).
