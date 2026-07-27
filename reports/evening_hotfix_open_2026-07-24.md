# Evening APP hotfix open — 2026-07-24

**Mission:** LEARNING_LOOP_PLAN Step 2 APP hotfixes → flat dual recycle → SB evening arm (~4h).  
**Generated:** 2026-07-24T18:42 BST  
**Witness:** `src/data/v31-production/state/operator_reopen_witness.json`  
**day_net_at_reopen_gbp:** −379.08 (111 closes — calendar SoT; wrong −347 stamp briefly caused auto-relock)

---

## Pre-dev audit

| Check | Result |
|---|---|
| Books | **FLAT** both ports (0 / 0) |
| Trading paused (pre) | CFD Y · SB Y |
| Watchdog hold | Engaged for anti-zombie recycle only |
| kill -9 | **Not used** |

---

## Hotfixes shipped (APP only)

| # | Fix | Where |
|---|---|---|
| 1 | **Path A soft-loss min-hold** — defer `soft_loss` / soft GBP flatten until `min_hold_before_trail_sec` (150s); hard `loss_cap` still fires | `execution/macro_path_a_exit_guard.py`, `runtime/micro_gbp_exit.py`, `execution/open_position_rules.py` |
| 2 | **Kill Scalping BE+tx under Path A** — MACRO_SENTINEL / SB suppresses scalping BE arming (404 ghost class) | `execution/scalping/config.py`, `trading/trade_manager.py` |
| 3 | **Journal stamps** — HoldSec from learning DB span; MlScore from confidence; MarketRegime → `UNKNOWN` fallback | `diagnostics/performance_journal.py`, `data/learning_store.py` |
| 4 | **Epic hard policy** — `exclude_from_hot_path` wins over failover; last-line reject on `place_market_order` (Nikkei/DAX + SB allowlist) | `runtime/dual_core_execution.py`, `ig_api/rest_client.py` |
| 5 | **A2 CFD** — confirmed hard-block + fail-closed (marker + tests) | `api/agent_control.py` (unchanged logic, re-verified) |
| 6 | **Supervisor** — MICRO_HOLD FAIL now sets `ensure_bleed_halt` (force pause + lock); BLEED/SESSION_KILL unchanged | `runtime/gui_desk_supervisor.py` |

**Not done (per plan):** Instant/micro stay **HARD OFF**. OBI fail-open not loosened. No LOGIC parameter loosen.

---

## Tests

```text
tests/test_app_evening_hotfixes_2026_07_24.py
tests/test_a2_pause_entry_hard_block.py
→ 22 passed in ~43s
```

No UI / tsc (backend only).

---

## Deploy / posture

| Port | Role | PID | trading_paused | Intent |
|---|---|---:|---|---|
| `:8080` | CFD QUANT_SNIPER | **24412** | **True** | A2 — keep paused |
| `:8081` | SB MACRO_SENTINEL | **24671** | **False** | Evening armed |

- A2 marker: `state_cfd/a2_entries_paused.json` **active=true** `mode=A2_SB_ONLY`
- Instant/micro: `sb_disable_instant_micro` / `sb_disable_core_b_micro` / `sb_macro_ltr_entries_only` = **true** (config)
- Ranked: **active** (DOW + EURUSD promoted at open — ranked OK, exclude still hard-blocks Nikkei/DAX)
- Bleed locks: **cleared**; witness baseline = calendar day net **−£379.08** so prior SESSION_KILL stays WATCH (no instant re-lock). Fresh adverse closes / day-net worsening still auto-lock.
- Anti-zombie: `v32_runtime_start.sh stop` → bytecode eviction → `start` (no kill -9)
- Ops note: first witness used incomplete journal net (−347) → supervisor `day_worsened=true` → relock loop; restamped to calendar SoT.

---

## Watch criteria (first 30–60 min)

1. **Stamped closes** — HoldSec + MlScoreAtEntry + MarketRegime present on SB closes.
2. **No soft_loss &lt;150s** on MACRO_SENTINEL / style=macro (hard cap OK).
3. **No Scalping BE+tx** notes on SB Path A closes.
4. **No Nikkei/DAX** fills (policy reject).
5. **CFD stays paused** — A2 marker + `trading_paused=true`.
6. **MICRO_HOLD / BLEED / SESSION_KILL** — fresh FAIL → auto-pause SB again + durable locks (never auto-resume).
7. Day net must not worsen vs **−£379.08** by ≥£1 (session kill baseline).

### Kill switch

```bash
curl -sS -X POST http://127.0.0.1:8081/api/stop
curl -sS -X POST http://127.0.0.1:8080/api/stop
# supervisor ensure_operator_bleed_halt also writes locks on threshold
```

Do not re-enable Instant/micro. Do not lift A2 on `:8080`.

---

## Deliver snapshot

| Item | Value |
|---|---|
| Hotfixes | 6 APP (above) |
| Tests | **22 passed** |
| CFD PID | 24412 · paused **Y** |
| SB PID | 24671 · armed **Y** |
| Instant/micro | **OFF** |
| Flat | **Y** both |
