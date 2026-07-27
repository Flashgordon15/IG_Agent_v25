# GUI Desk Supervisor — Phase 1 + Phase 2

Permanent **observe + score + resolve queue** for the Trading Desk GUI path, plus **allowlisted self-heal only**.

| Allowed | Forbidden |
|---------|-----------|
| Curl health / positions / trade_support / accounting / stability / ops_strip / rotation / state | Raise REST 3/min hard cap |
| Score `PASS` \| `WATCH` \| `FAIL` / chip `HALTED` | Re-enable Instant/micro / loosen ElasticGate / OBI fail-open |
| Write SoT JSON + MD + jsonl history + silence/flicker trackers | Strategy / alpha rewrites / `allow_non_dow` unlock |
| `cursor_handoff` when `needs_code` or ops-critical BLEED/HALTED | `kill -9` / SIGKILL |
| Desk chip with plain tags: **BLEED / MICRO_HOLD / GUI_LIE / HALTED** | Lifting A2 / bleed lock via `POST /api/start` |
| Phase-2 allowlisted heals (table below) incl. `ensure_operator_bleed_halt` | Heal after **2/hour** cap (hard FAIL + escalate) |
| Force pause + write bleed locks when BLEED/SESSION_KILL while unlocked | Remove `operator_bleed_lock_*.json` without operator unlock |

Reopen criteria (manual only): [`docs/DESK_REOPEN_CHECKLIST.md`](DESK_REOPEN_CHECKLIST.md).

---

## Operator commands

```bash
# Enable (install LaunchAgent + first run)
./scripts/install_gui_desk_supervisor.sh --enable

# Disable
./scripts/install_gui_desk_supervisor.sh --disable

# Status + last score
./scripts/install_gui_desk_supervisor.sh --status

# Manual one-shot (observe + write SoT; may ensure bleed halt if unlocked)
./scripts/install_gui_desk_supervisor.sh --run-once

# Heal dry-run (plan only)
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/gui_desk_supervisor.py --heal-dry-run

# Execute allowlisted heals (flat books required for recycle; never start under bleed lock)
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/gui_desk_supervisor.py --heal

# G2G live verify
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/verify_gui_desk_supervisor_g2g.py --heal-dry-run
```

LaunchAgent label: `com.igagent.gui_desk_supervisor`  
Schedule: `StartInterval` **120s** + `RunAtLoad`  
Plist template: `scripts/com.igagent.gui_desk_supervisor.plist`

Default LaunchAgent run is **observe-only** (+ automatic `ensure_operator_bleed_halt` when BLEED/SESSION_KILL while unlocked, or when locks exist but a port is not paused). Set `IG_GUI_SUP_AUTO_HEAL=1` only if you want dry-run planning every cycle; use explicit `--heal` for other mutations.

---

## Source of truth (SoT)

| Path | Role |
|------|------|
| `src/data/v31-production/state/gui_supervisor_latest.json` | Machine-readable score + ranked findings + chip + heal |
| `src/data/v31-production/reports/gui_supervisor_latest.md` | Operator markdown |
| `src/data/v31-production/state/gui_supervisor_history.jsonl` | Append-only compact history |
| `src/data/v31-production/state/gui_supervisor_silence.json` | ARMED silence timer state |
| `src/data/v31-production/state/gui_supervisor_flicker.json` | prefer/SETUP flip tracker |
| `src/data/v31-production/state/gui_supervisor_heal_log.jsonl` | Heal audit |
| `src/data/v31-production/state/gui_supervisor_heal_budget.json` | 2/hour heal cap window |
| `src/data/v31-production/state_{cfd,sb}/operator_bleed_lock_*.json` | Durable do_not_auto_resume |

Code: `src/runtime/gui_desk_supervisor.py` + `src/runtime/gui_desk_supervisor_heal.py`  
CLI: `scripts/gui_desk_supervisor.py`  
Chip: `terminal/src/components/gpu/GuiSupervisorChip.tsx` (mounted under SYSTEM OPERATIONAL in `GpuPlatformShell`)  
API: `terminal/src/app/api/desk/gui_supervisor/route.ts`

---

## Phase 1 + Phase 2 integrity checks

### Classic plane

1. `:8080` / `:8081` — health, positions/live, trade_support, liveness, stability, ops_strip, accounting, rotation_state, state
2. **Loops armed / `accepting_ticks`** (STUCK if READY/trade_ready but dormant; A2 pause on CFD is ignore)
3. **Path A carve vs MICRO block on SB**
4. **Ranked promote** vs `exclude_from_hot_path` / `ranked_candidate_epics`
5. **A2 CFD pause** marker vs `trading_paused` on `:8080`
6. **Zero-attempt / silence timer** — SB ARMED with no activity for `IG_GUI_SUP_SILENCE_MINUTES` (default **30**)
7. **ml_confidence warm**
8. **Cash merge** / blotter / REST / `:3000` / hung API

### Desk integrity plane (catches bleed / GUI lies first)

| Check | Trigger | Score | Side effect |
|-------|---------|-------|-------------|
| **HALTED** | Active `operator_bleed_lock_*.json` with `do_not_auto_resume` | **FAIL** · chip `SUPERVISOR HALTED · BLEED LOCK` | Never PASS; heals block `/api/start` |
| **BLEED** | Recent journal closes (window `IG_GUI_SUP_BLEED_WINDOW_MINUTES`, default 180) — n≥`BLEED_MIN_TRADES`, WR &lt; `BLEED_MAX_WR` (0.25) **or** net ≤ `BLEED_MAX_NET_GBP` (−50) | **FAIL** · `needs_ops` | If unlocked → `ensure_operator_bleed_halt` (stop both + write locks) |
| **SESSION_KILL** | Calendar-day journal net ≤ `IG_GUI_SUP_SESSION_KILL_NET_GBP` (default **−150**) | **FAIL** · `needs_ops` | Same ensure halt if unlocked |
| **MICRO_HOLD** | Median/avg `HoldSec` below thresholds while Path A / macro carve claimed | **FAIL** · `needs_code` | Telemetry missing → WATCH |
| **GUI_LIE** | prefer/SETUP while paused; or Intent SETUP vs SB aggregate WAIT | **FAIL/WATCH** · `needs_code` | `cursor_handoff` |
| **FLICKER** | prefer/SETUP flip count ≥ `IG_GUI_SUP_FLICKER_MAX_FLIPS` in window | **WATCH** · `needs_code` | Tracker: `gui_supervisor_flicker.json` |
| **POST_CUTOVER_OUTCOME** | Last N min closes net-neg with short/missing holds | **FAIL/WATCH** | Never score green PASS / PIPELINE_OK |
| **EPIC_POLICY** | Close / prefer / promote on `exclude_from_hot_path` (e.g. Nikkei) | **FAIL** | |

Findings ranked with `class`: `ops` | `ui` | `code` | `strategy` | `ignore`, plus `needs_code` / `needs_ops`.

When `needs_code` (e.g. **GUI_LIE**) or ops-critical **BLEED/HALTED/SESSION_KILL**, JSON includes `cursor_handoff`.

Dashboard chip: silent on PASS; amber **WATCH** / red **FAIL** or **HALTED · BLEED LOCK** with plain alert tags.

---

## Phase 2 — heal allowlist

| Action | Trigger | Behaviour | Flat books? |
|--------|---------|-----------|-------------|
| `port_hung_soft_recycle` | LISTEN + API timeout on CFD/SB | `mark_manual_stop` → **SIGTERM only** → clear locks → spawn that port; re-apply A2 `/api/stop` on CFD; `/api/start` SB only if armed **and no bleed lock** | **Yes** (else escalate) |
| `loops_not_arming_unpause_or_recycle` | READY/trade_ready but `accepting_ticks=false` | `/api/start` unpause **blocked under bleed lock**; if still stuck and flat → one soft recycle | Recycle yes / unpause no |
| `ui_restart_only` | `:3000` down | `launchctl kickstart com.igagent.v30.ui` and/or `start_ui_background.sh` | No |
| `armed_silence_soft_pause_sb` | Silence FAIL | Optional `POST :8081/api/stop` (halt bleed) — **does not loosen gates** | No |
| `reapply_a2_cfd_pause` | A2 marker active, CFD not paused | `POST :8080/api/stop` + stamp marker | No |
| `ensure_operator_bleed_halt` | BLEED / SESSION_KILL / unlocked bleed | `POST /api/stop` both + write `operator_bleed_lock_*.json` — **never** `/api/start` | No |

**Cap:** 2 heals / hour → hard FAIL + ops escalate.

**Never:** `kill -9`, raise REST, re-enable Instant/micro, loosen ElasticGate/OBI, strategy rewrites, `allow_non_dow` unlock, lift A2 / bleed lock with `/api/start`.

---

## How Cursor reads the queue

1. Open `src/data/v31-production/state/gui_supervisor_latest.json` (or `.md` twin).
2. If chip is **HALTED / BLEED LOCK** → ops first; do not resume; see reopen checklist.
3. If `needs_code` → use `cursor_handoff` (GUI_LIE / MICRO_HOLD / …).
4. If `needs_ops` → BLEED / SESSION_KILL / pause re-engage / UI.
5. Always honour `a2.preserve` and bleed locks.
6. Inspect `heal.plans` / `bleed_halt` after run-once / `--heal`.

---

## Scoring notes

- **FAIL / HALTED** — bleed lock present, BLEED/SESSION_KILL thresholds, GUI_LIE prefer-vs-paused, MICRO_HOLD under Path A, EPIC_POLICY, STUCK loops, silence timer, heal cap, hung agent/UI, positions CRITICAL, cash double-count, A2 marker but CFD not paused, REST CRITICAL.
- **WATCH** — FLICKER, POST_CUTOVER soft, REST ELEVATED, thin blotter, hold telemetry missing, ranked config ON but inactive.
- **PASS** — path live, flat/healthy opens, no integrity alerts, A2 consistent, UI up, loops accepting on armed SB.

Expected soft signals under intentional pause (often **ignore** for health ok=false): `trading_paused` / `watchdog_inactive` alone — but **HALTED/BLEED lock must never look like PASS**.

---

## Env knobs (defaults)

| Env | Default | Meaning |
|-----|---------|---------|
| `IG_GUI_SUP_BLEED_WINDOW_MINUTES` | 180 | Recent-close window |
| `IG_GUI_SUP_BLEED_MIN_TRADES` | 5 | Min sample for BLEED |
| `IG_GUI_SUP_BLEED_MAX_WR` | 0.25 | FAIL if WR below |
| `IG_GUI_SUP_BLEED_MAX_NET_GBP` | -50 | FAIL if window net ≤ |
| `IG_GUI_SUP_SESSION_KILL_NET_GBP` | -150 | Day net kill floor |
| `IG_GUI_SUP_MICRO_HOLD_MEDIAN_SEC` | 60 | Path A median hold floor |
| `IG_GUI_SUP_MICRO_HOLD_AVG_SEC` | 90 | Path A avg hold floor |
| `IG_GUI_SUP_POST_CUTOVER_MINUTES` | 30 | Post-cutover window |
| `IG_GUI_SUP_FLICKER_MAX_FLIPS` | 6 | prefer/SETUP flips → WATCH |
| `IG_GUI_SUP_SILENCE_MINUTES` | 30 | ARMED silence |

---

## G2G gate

```bash
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_gui_desk_supervisor.py -q
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/verify_gui_desk_supervisor_g2g.py --heal-dry-run
# if terminal touched:
cd terminal && npx tsc --noEmit && npm run build
```

G2G for live trading requires score PASS or WATCH-only with no unfixed STUCK_BUG.  
**Under operator halt / bleed lock, score MUST be FAIL/HALTED (never false PASS).**
