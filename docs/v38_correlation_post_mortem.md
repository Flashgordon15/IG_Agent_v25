# v38 Correlation / Cash Post-Mortem — Phase 0 Forensics ONLY

**When:** 2026-07-24 ~11:29–11:35 Europe/London  
**Mode:** READ-ONLY (curl + journals/snapshots/logs). No restart, kill, deploy, orders, or config changes.  
**Daylight witness:** left alone (`daylight_session_witness.py --until 16:00`, PID 23210).

---

## Pre-dev audit (session start)

| Check | Result |
|---|---|
| Market sessions closed? | **No** — daylight session; loops armed / trade_ready |
| Watchdog hold active? | **No** manual-stop marker found |
| Active PIDs (start) | **:8080 = 55365** (Z6BAH4 CFD) · **:8081 = 55628** (Z6BAH3 SB) |
| Open books (start) | **FLAT** both ports (`verdict=FLAT`, `critical=false`, `broker_open=0`) |
| Mid-forensics drift | **:8080 unbound** — PID 55365 gone by ~11:33; **:8081 still 55628**. Not touched by this forensics. Stale `agent.pid` files still show `3966`. |

---

## Verdict

### **MIXED**

| Slice | Classification |
|---|---|
| GUI “True Settled Cash ≈ −£514” | **ACCOUNTING_ARTIFACT** (dual-port double-count of one shared journal day) |
| Overnight + day losses on Z6BAH3/Z6BAH4 | **EXPECTED_INDEPENDENT_DUAL_BIAS** (both SELL-heavy; separate deal IDs / engines) |
| True ±2s / ±60s mirror duplicate entries | **Not evidenced** — **0** reliable same-exit/same-PnL pairs within ±2s or ±60s |
| REST CRITICAL choking *right now* | **No** (`rate_limit_paused=false`; forensic 429/CRITICAL hits = 0 in current log; overnight 429s are historical) |

Primary operator confusion is the **cash label**, not an open-risk emergency and not a proven mutex mirror bug.

---

## Q1 — What does GUI “True Settled Cash ≈ −£514” map to?

### Plain English

It is **not** IG broker “available cash”, **not** account balance, and **not** open unrealized PnL.

It is the Terminal label for **today’s net realized PnL**, merged from both API ports — and that merge currently **adds the same journal total twice**.

### Evidence chain

1. **UI wiring** (`terminal/src/components/gpu/SovereignAccountingBoard.tsx`):
   - Label: `True Settled Cash`
   - Subtitle: `Today · net realized · dual-port merge`
   - Value painted from `merged.today_net_realized_pnl_gbp`

2. **Merge math** (`terminal/src/lib/desk-accounting-merge.ts`):
   ```ts
   today = cfd.today_net_realized_pnl_gbp + sb.today_net_realized_pnl_gbp
   ```

3. **Both ports returned the same payload** (live curl ~11:30):
   - `:8080/api/desk/simplified_accounting` → `today_net_realized_pnl_gbp: -258.91`, `source: "journal_csv"`
   - `:8081/api/desk/simplified_accounting` → `today_net_realized_pnl_gbp: -258.91`, `source: "journal_csv"`
   - GUI sum: **−258.91 × 2 = −517.82 ≈ operator’s “≈ −£514”**

4. **Why both ports match:** `simplified_accounting_payload()` reads the **shared** `src/data/v31-production/metrics/daily_journal.csv` with **no account filter** (`src/diagnostics/performance_journal.py` + `/api/desk/simplified_accounting` in `src/api/routes.py`). Dual processes, one journal file.

5. **Independent journal reconcile** (unique DealID, calendar `2026-07-24*`):
   | Account | Closes | Net £ |
   |---|---:|---:|
   | Z6BAH3 (SB) | 61 | **−179.22** |
   | Z6BAH4 (CFD) | 17 | **−79.69** |
   | **Combined (true)** | 78 | **−258.91** |
   | **GUI dual-merge (artifact)** | — | **−517.82** |

6. **Not broker cash / available:**
   - `dashboard_snapshot.balance_gbp = null`
   - `daily_pnl_gbp` / `realized_daily_pnl_gbp` on snapshot were `0.0` (stale/other path)
   - Broker snapshots: `count: 0`, `account_upl: null` (`state/`, `state_cfd/`, `state_sb/`)
   - Live positions: `total_pnl_gbp: 0.0`, flat

7. **Overnight context (real loss, already reported):** `trading_report_2026-07-24_0800.md` overnight net **−£215.93** (separate window; not the −£514 figure).

### Q1 conclusion

**ACCOUNTING_ARTIFACT.** Calm reading: treat “True Settled Cash” as **≈ −£259 today’s journal realized** (Z6BAH3 −£179 + Z6BAH4 −£80), not −£514 and not a broker cash hole.

---

## Q2 — Mirror duplicates vs independent dual bias?

### Design (expected)

| Port | Account | Engine | Product |
|---|---|---|---|
| :8080 | Z6BAH4 | QUANT_SNIPER | CFD |
| :8081 | Z6BAH3 | MACRO_SENTINEL | SPREADBET |

Architecture intends **independent dual-regime** desks (`docs/V37_DUAL_REGIME_OWNERSHIP.md`), not a locked 1:1 mirror.

### Timing test (overnight + calendar day journal)

1:1 match rule: same direction, exit within 0.55 pts, same signed PnL (±£0.02), index-like exits only (exclude FX noise).

| Bucket | Count |
|---|---:|
| Pairs within **±2s** | **0** |
| Pairs within **±60s** (excl. ±2s) | **0** |
| Pairs with ~**1h** journal timestamp skew (~3540–3590s) | **11** |
| Pairs with **midnight stub** (`T00:00:00Z`) on one side | **6** |
| CFD deals in window | 40 |
| SB deals in window | 66 |

Interpretation:

- **No clean evidence of unintentional same-epic+direction fills within ±2s/±60s.**
- Many “twins” share exit+PnL but timestamps are either **~1 hour apart** or **midnight broker_attached stubs** — journal/backfill pollution / correlated levels, **not** a proven live mutex race placing simultaneous duplicates.
- Overnight blotter already showed **independent** books: CFD 22 scalp closes (−£92.57, 0 wins) vs SB 44 longer closes (−£123.36, 9 wins) — same **SELL bias**, different cadence/engines.
- Correlation guard file present: `state/correlation_guard.json` → `{buy:0,sell:0,...}` (flat session); `.stop_snapshot_mirror` active since desk_deploy 2026-07-22 (snapshot mirror stop, not trade mutex).

### Q2 conclusion

**EXPECTED_INDEPENDENT_DUAL_BIAS** for the economic losses (both desks losing on DOW-directionally similar SELL pressure overnight/day).  
**Not** classified as **UNINTENTIONAL_MIRROR_BUG** on entry timing evidence available in Phase 0.

---

## Q3 — Is REST CRITICAL / rate-limit choking *right now*?

### Evidence at forensics time (while both ports answered)

| Signal | Value |
|---|---|
| `/api/positions/live` `rate_limit_paused` | **false** (8080 & 8081) |
| `critical` / `verdict` | **false** / **FLAT** |
| `state/forensic_network.log` current 429/CRITICAL matches | **0** |
| Shared REST stamps (approx) | `ig_positions` last_60s ≈ 2–3 (not a storm); yahoo poller busy (expected) |
| Overnight report (historical) | `feed_429_hits=274` — **past** pressure, not current choke |

`/api/state` showed `rest_budget: 0` with `stream_status: LIVE` / `agent_state: CAUTION` — not used alone as CRITICAL proof; positions path was healthy and not rate-limit paused.

### Q3 conclusion

**No — REST is not critically choking bad behavior right now.** Overnight 429 noise is real history; current open-path is not in rate-limit pause.

---

## OPS ADVICE (advisory only — nothing executed)

### Should the operator halt new entries now?

- **Not as an emergency flatten** — books were **already flat**; no open critical risk.
- **Optional soft pause is reasonable** if the goal is to stop further dual SELL-bias losses after overnight FAIL (−£216) and day journal ≈ −£259 — that is a **risk/psychology** choice, not because of open positions or REST CRITICAL.
- Do **not** halt because of the −£514 GUI number; that figure is the double-count artifact.

### Are books currently open / critical?

- **At Phase 0 start:** both ports **FLAT**, `critical=false`, `broker_open=0`.
- **At Phase 0 end:** **:8080 down** (55365 gone); **:8081 still flat** (`state_sb/broker_snapshot.json` count 0). No evidence of stuck opens on snapshots.
- Daylight witness still running — left alone per constraints.

### Single next action after this report

**Treat True Settled Cash as ≈ −£259 journal day (ignore −£514 as double-count), leave witness alone, then decide whether to soft-pause new entries for the losing dual SELL bias — and separately notice :8080 has drifted down (55365) while forensics stayed read-only.**

(Any later recovery of :8080 must follow anti-zombie / flat-book deploy rules — **not** part of this Phase 0.)

---

## GUI fix (2026-07-24) — shared-journal double-count

**Status:** code + tests only (no agent restart / deploy this turn).

| File | Change |
|---|---|
| `terminal/src/lib/desk-accounting-merge.ts` | Detect shared global journal clone (`isSharedGlobalJournalClone`); combined `today_net_realized_pnl_gbp` / daily history / perf taken **once** when :8080 and :8081 both return the same unfiltered `journal_csv` aggregate; still **sums** when payloads look like distinct account contributions |
| `terminal/src/lib/desk-weekly-metrics-merge.ts` | Same pattern for weekly: shared clone → one payload; else roll up by distinct `account_id` (never sum the same AccountID twice) |
| `terminal/src/components/gpu/SovereignAccountingBoard.tsx` | Source chip shows `SHARED JOURNAL · ONCE` when deduped; subtitle notes dedupe |
| `terminal/src/lib/desk-accounting-merge.selftest.ts` | Focused assert: −258.91×2 → −258.91; distinct −79.69+−179.22 → −258.91 |

**Legacy dashboard:** no “True Settled Cash” / dual-port simplified_accounting merge found — Quantum Terminal only.

**Verify:** from `terminal/`: `npx --yes tsx src/lib/desk-accounting-merge.selftest.ts` and `npx tsc --noEmit`. UI cutover: Next may hot-reload on `:3000`; static/prod Terminal needs a flat UI refresh — **do not** restart trading agents for this GUI-only fix.

---

## What NOT to build (defer)

Do **not** implement a ±2s / 60s CFD→SB entry mutex (`anti_correlation.lock`) from this post-mortem — forensics found **0** reliable mirror pairs; dual SELL bias is **portfolio risk / regime correlation**, not a proven process duplicate bug. Keep REST under the existing shared `RestApiBudget` / `shared_rest_budget` path — do not invent a second rate-limiter. Soft-pause or size-cut for losing dual bias is an operator risk choice, not a mutex patch.

---

## Parent summary (one sentence)

**Cash truth ≈ −£259 today realized (GUI −£514 = shared-journal double-count), mirror verdict = MIXED / expected independent dual SELL bias not ±2s mutex duplicates, REST not CRITICAL now, books flat with :8080 later unbound — next: believe −£259 and choose soft entry pause only for the losing bias, not for open risk.**
