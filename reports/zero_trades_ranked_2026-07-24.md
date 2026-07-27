# Zero trades after G2G — read-only NOW check — 2026-07-24 ~14:15 BST

**Verdict NOW: EXPECTED_WARMING** (not STUCK_AGAIN, not OVER_GATED beyond design).

No restart. Books FLAT both ports. Supervisor latest **PASS** (checked `2026-07-24T14:14:35+01:00`). Silence held (~12 min / 30 min threshold) because vector/signal still warm.

## Pre-dev audit (14:15 BST)

| Check | Result |
|---|---|
| Market / book | **FLAT** — `broker_open=0`, positions `verdict=FLAT` both ports |
| Watchdog hold | Not engaged for deploy; health notes `watchdog_inactive` (soft) |
| Active PIDs | CFD **29818** :8080 · SB **30120** :8081 (~24 min up) — clean, accepting ticks |

## 1) PIDs / health / pause / ticks

| Port | PID | Role | trading_paused | trade_ready | loops |
|---|---|---|---|---|---|
| :8080 | 29818 | CFD QUANT_SNIPER | **true (A2 — preserve)** | true | built=7, running, **accepting_ticks=true** |
| :8081 | 30120 | SB MACRO_SENTINEL | **false (armed)** | true | built=7, running, **accepting_ticks=true** |

GUI “no trades” on CFD is expected (A2 pause). Fill path is SB only.

## 2) ML / vector / signal (SB :8081)

| Field | Value |
|---|---|
| boot label | `Compiling Vector Arrays: 1024 / 1792 Bars… [57%]` (`boot_ready=false`) |
| `ml_confidence` | **0.0** |
| `signal_strength` | **0.0** (threshold 55) |
| `agent_state` | HEALTHY (not SETUP) |
| `/api/signals` | `[]` |
| gate notes | `warm_up_complete=true` / `operational_ready=true` but readiness label still 57% — **signal plane still cold** |

**Blocking fill path:** Path A carve is open, but there is no SETUP — confidence/strength still zero while vectors warm. Supervisor treats this as expected (`sb_signal_warm=PASS`).

## 3) Logs since cutover (~13:51)

- All 7 SB loops: `entering tick loop` at 13:51:45 — **not dormant**.
- **Path A entry attempts: 0** (no `execute_entry` / `placeOrder` / `ORDER_OPEN`).
- ParallelStrategySweep since ~13:51: pierce zones fire → **only micro path tried** → veto `sb_core_b_micro_hard_disabled` (by design). Nikkei → `hot_path_epic_excluded`.
- No Path A dispatch/veto storm — Path A never reached an entry attempt because signal/ML never left zero.

## 4) Ranked promote

Still allowing Gold + DOW entries (allowlist side OK):

- `ranked_rotator.active=true`
- `promoted=[IX.D.DOW.IFM.IP, CS.D.CFPGOLD.CFP.IP]`
- `reason=ranked_top2:DOW@100.5, CFPGOLD@100.5`

DOW + Gold hard carve: `hard_allow=[PATH_A]`, `hard_block=[MICRO, PATH_B_HANDOFF]`. Instant/micro remains hard-off (intentional).

## 5) Supervisor latest

| Item | Value |
|---|---|
| Score | **PASS** |
| `sb_armed_silence` | PASS (silence ~12 min < 30 min; held because `boot_ready=false`) |
| `sb_signal_warm` | PASS — `ml_confidence=0` while vector warm (info/ignore) |
| `sb_path_a_carve` | PASS |
| `ranked_allowlist` | PASS |
| Chip | SUPERVISOR PASS / all clear / not visible |

Does **not** see STUCK. Sees expected warm silence.

## 6) Verdict ladder

| Label | Fit |
|---|---|
| **EXPECTED_WARMING** | **YES — primary.** Zero fills because signal/ML cold (`ml=0`, strength=0), not because loops/carve/ranked broken. |
| EXPECTED_SELECTIVE | Secondary once warm — Instant/micro hard-off means only Path A setups can fill; pierce-zone micro hits will keep vetoing. |
| STUCK_AGAIN | **No.** Loops tick; carve live; supervisor PASS; silence under threshold. |
| OVER_GATED | Design posture only (micro hard-off + A2 CFD pause) — not a new accidental gate. |

## 7) Restart / fix?

**No.** Books flat, but not clearly stuck — do **not** flat-restart for selectivity/warming.

### Operator — plain English

**What’s blocking:** SB is armed and ticking, Path A is allowed for DOW+Gold, ranked promote is fine. The brain is still warming — `ml_confidence` and `signal_strength` are still **0**, so nothing reaches SETUP/fill. Micro/Instant stays hard-off on purpose, so those pierce-zone blips will not trade. CFD is paused on purpose (A2).

**What to do:**
1. **Wait** for vector warm to finish and `ml_confidence` / `signal_strength` to leave 0 (watch `:8081/api/state` or GUI).
2. Do **not** restart unless supervisor flips to silence FAIL / STUCK after warm completes with confidence still glued at 0 and zero Path A attempts for 30+ min post-warm.
3. Do **not** re-enable Instant/micro to “force” fills.
4. Expect fills only on SB Path A (DOW/Gold) once SETUP appears; CFD stays silent under A2.

### Watch (escalate only if…)

- After boot shows ready / vectors complete, `ml_confidence` stays 0 for another ~15–20 min **and** supervisor `sb_armed_silence` fails → re-diagnose as STUCK_AGAIN.
- Vector label frozen at `1024/1792` forever while ticks flow — cosmetic/progress stall to investigate later; not a restart trigger while supervisor PASS + loops accepting.

---

# Historical — cutover notes (~13:43–13:52 BST)

**Verdict (initial): STUCK_BUG** (primary) + **OVER_GATED** (secondary). Not EXPECTED_QUIET.

**Post-restart (~13:52 BST): STUCK_BUG cleared; Path A carve live on SB.** Remaining: signal plane cold while vector arrays warm; Instant/micro still hard-off (by design).

## Pre-dev audit (cutover)

| Check | Result |
|---|---|
| Market / book | **FLAT** both ports (`count=0`, verdict FLAT) before and after restart |
| Watchdog hold | Engaged via `v32_runtime_start.sh stop` (`mark_manual_stop`), then cleared on start |
| PIDs (pre) | **9913 → :8080**, **10190 → :8081** (stale RAM — dormant loops) |
| PIDs (post) | **29818 → :8080**, **30120 → :8081** |

## Desk map (post-restart)

| Port | PID | Account | Origin | Role | trading_paused | trade_ready | loops |
|---|---|---|---|---|---|---|---|
| :8080 | 29818 | Z6BAH4 | QUANT_SNIPER | CFD | **true (A2)** | true | built=7, running, **accepting_ticks=true** |
| :8081 | 30120 | Z6BAH3 | MACRO_SENTINEL | SB | **false** | true | built=7, running, **accepting_ticks=true** |

Order valve: CFD SUPPRESSED (`/api/stop`); SB READY (`/api/start`). Books still FLAT.

## Ranked rotator — still OK

`/api/rotation_state` → `rotation.ranked_rotator` on SB:

- `active=true`, `mode=ranked`, `dominant=DOW`
- **`promoted=[DOW, Gold]`**
- Reason: `ranked_top2:DOW@62.0, CFPGOLD@62.0` (later ~100.5)

## Why zero fills (ranked causes) — historical

1. **STUCK_BUG — V6 post-READY dormant TradingLoops (macro/LTR dead)** — **FIXED in RAM**  
   Prior PIDs: Gate5 READY → materialize with `paused_at_boot=True` → zero `entering tick loop`.  
   Fix on disk: `V6InLoopCoroutineHandoff` post-READY `unpause_from_boot()` (`market_orchestrator.py`).  
   **After dual cutover (13:51:45 BST):** all 7 SB loops logged `awaiting stream_ready` → **`entering tick loop`** (no stuck dormant). API `accepting_ticks=true`.

2. **OVER_GATED — SB policy vs SCALP hard enforcement** — **CARVE LOADED**  
   Config still: `sb_disable_instant_micro` + `sb_disable_core_b_micro` + `sb_macro_ltr_entries_only`.  
   **New:** `sb_macro_path_a_carve_active` — on MACRO_SENTINEL, SCALP ownership allows **PATH_A** and hard-blocks **MICRO** (does not re-enable Instant/Core-B).  
   Live SB DOW hard enforcement: `hard_allow=[PATH_A]`, `hard_block=[MICRO, PATH_B_HANDOFF]`.  
   CFD DOW unchanged: `hard_allow=[MICRO]`, `hard_block=[PATH_A, PATH_B_HANDOFF]` (no carve).

3. **Expected / not root**  
   - CFD A2 pause on :8080  
   - Instant/Core-B micro hard-off on SB (retained)  
   - Gate4 / vector compile still warming (`Compiling Vector Arrays… ~57%`) — `ml_confidence=0`, `signal_strength=0` until bars finish  
   - Ranked promote ≠ velocity stack rotation (OK)

## Fixes applied this cutover

| Item | Status |
|---|---|
| V6 post-READY unpause (`market_orchestrator.py`) | Loaded in new PIDs — loops entered tick loop |
| SB Path A carve (`dual_regime.sb_macro_path_a_carve_active` + hard/soft/controller) | Live on :8081 |
| Tests | `test_hard_enforcement` Path A carve + CFD isolation; `test_strategy_controller` carve; `test_v6_handoff_unpause` |
| Dual anti-zombie | `v32_runtime_start.sh stop` → bytecode eviction → `start` (no kill -9) |
| Posture | CFD `/api/stop`; SB `/api/start` |
