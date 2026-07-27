# Desk reopen checklist (DOW-only) — no auto start

**Operator-only.** Supervisor / Phase-2 heal must **never** `POST /api/start` while
`operator_bleed_lock_*.json` with `do_not_auto_resume` is present.

Use this after a BLEED / SESSION_KILL / HALTED score. Removing locks ≠ resume;
resume is a separate explicit curl per port.

---

## Preconditions (all required)

1. Read `src/data/v31-production/reports/gui_supervisor_latest.md` — understand why halted.
2. Books **FLAT** both ports: `GET /api/positions/live` → `count=0`, verdict FLAT/HEALTHY.
3. Day / window journal reviewed — net and WR acceptable vs desk gates (not still bleeding).
4. Bleed locks still present until you consciously remove them (both CFD + SB).
5. Hot path remains **DOW only** until Nikkei JPY PnL certified (`dual_core.exclude_from_hot_path`).
6. Instant/micro stays **off**; Path A carve on SB preserved; REST 3/min untouched.

---

## Unlock (explicit)

```bash
# 1) Stamp witness watermark FIRST (prevents pre-halt journal from instantly re-locking)
#    Written automatically by reopen procedure as state/operator_reopen_witness.json

# 2) Remove BOTH locks only after review
rm src/data/v31-production/state_cfd/operator_bleed_lock_YYYY-MM-DD.json
rm src/data/v31-production/state_sb/operator_bleed_lock_YYYY-MM-DD.json

# 3) Resume ports (operator curl — Instant/micro stay HARD OFF)
curl -sS -X POST http://127.0.0.1:8081/api/start
curl -sS -X POST http://127.0.0.1:8080/api/start

# 4) Confirm trading_paused=false; supervisor may WATCH prior SESSION_KILL/BLEED
#    but auto-lock only on NEW adverse closes after witness stamp
PYTHONPATH=src IG_DATA_ROOT=src/data/v31-production \
  .venv/bin/python3 scripts/gui_desk_supervisor.py --dry-run --json-stdout | head
```

**Witness policy:** `operator_reopen_witness.json` holds `reopened_at_epoch` + `day_net_at_reopen_gbp`.
Supervisor still surfaces prior damage, but `ensure_bleed_halt` only fires on **fresh**
closes after that stamp (or day net worsening vs reopen baseline).

---

## Post-reopen watch (first 30–60 min)

| Check | Expect |
|-------|--------|
| Chip / supervisor | Not HALTED; investigate any BLEED / MICRO_HOLD / GUI_LIE / POST_CUTOVER |
| Prefer / SETUP | No SETUP flash while paused; no prefer vs WAIT contradiction |
| Holds | Macro/Path A holds not sub-minute median |
| Excluded epics | No Nikkei promote/prefer/close on hot path |
| Session kill | Day net still above `IG_GUI_SUP_SESSION_KILL_NET_GBP` (default −£150) |

If any FAIL returns, stop both and re-engage locks — do not “one more probe.”
