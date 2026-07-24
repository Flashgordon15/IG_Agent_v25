# Ranked Multi-Market Rotator (SB hot path)

**Status:** LIVE on DEMO (`rotation_failover_enabled: true`, `ranked_rotator_mode: true` as of 2026-07-24)  
**Owner lane:** SB `MACRO_SENTINEL` (`:8081`) — CFD A2 pause unchanged  
**Constraint:** do **not** re-enable Instant / Core-B micro on SB

Supersedes the earlier DOW→Gold-only escape hatch. Strongest / most tradeable market becomes dominant; weaker markets rank lower. DOW is **not** permanently privileged.

## Problem

Static SB posture pinned entries to DOW via:

- `dual_core.sb_hot_path_allowlist: [DOW]`
- `selectivity_gates.allow_non_dow: false`

Gold / EURUSD / FTSE can sit at the top of rotation scores while SB could not flex. A Gold-only failover still privileged DOW as the primary until it went stale.

## Policy (ranked rotator)

### Candidates

Config-driven `ranked_candidate_epics` (default):

1. `IX.D.DOW.IFM.IP`
2. `CS.D.CFPGOLD.CFP.IP`
3. `CS.D.EURUSD.CFD.IP` (SB wire → `CS.D.EURUSD.TODAY.IP` via `broker_epic_resolver`)
4. `IX.D.FTSE.IFM.IP`

**Never promote:** anything on `exclude_from_hot_path` (Nikkei until JPY PnL certified, plus GBPUSD / DAX / Crude).

### Score

Per candidate (cheap, never blocks on missing history):

| Component | Role |
|---|---|
| Rotation composite | Live vol / spread / feed / regime score from dual-core |
| Eligible bonus | +12 when in rotation eligible/active (or no snapshot) |
| Journal expectancy tilt | Optional ±15 from recent `daily_journal.csv` mean PnL |

### Promotion / demotion

1. `dual_core.rotation_failover_enabled == true`
2. `ranked_rotator_mode == true`
3. Rank candidates each sweep (min interval `ranked_rerank_min_sec`, default 5s)
4. Promote top `ranked_promote_top_n` (default **2**) onto the **effective** SB allowlist — **replaces** static DOW-only base
5. Dominant = rank #1 → surfaced on routing / Desk Intent focus
6. Weaker markets (including DOW) drop off the allowlist when outside top-N

Selectivity / ElasticGate `*_non_dow_rejected` is skipped **only** for promoted epics (not a global `allow_non_dow: true`).

Still respect: Gold **10 £/pt**, EURUSD **1 £/pt**, DOW/FTSE **0.5**, overnight bans, SB Instant/micro HARD OFF, OBI fail-closed, REST budget, hard open cap **1**.

### Legacy DOW-stale mode

If `ranked_rotator_mode: false`, previous behaviour remains: after DOW WAIT / low confidence for `rotation_failover_stale_minutes`, union `failover_epics` onto the DOW base; clear after `rotation_failover_recover_minutes` of DOW recovery.

## Config knobs

```json
"dual_core": {
  "rotation_failover_enabled": true,
  "ranked_rotator_mode": true,
  "ranked_promote_top_n": 2,
  "ranked_rerank_min_sec": 5.0,
  "ranked_use_journal_expectancy": true,
  "ranked_candidate_epics": [
    "IX.D.DOW.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "IX.D.FTSE.IFM.IP"
  ],
  "failover_epics": [
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "IX.D.FTSE.IFM.IP"
  ],
  "exclude_from_hot_path": [
    "CS.D.GBPUSD.CFD.IP",
    "IX.D.DAX.IFM.IP",
    "CS.D.CRUDE.CFD.IP",
    "IX.D.NIKKEI.IFM.IP"
  ]
}
```

## LTR / Trend-Retention (companion)

SB longer trades stay armed via:

- `dual_regime.sb_disable_instant_micro` / `sb_disable_core_b_micro` / `sb_macro_ltr_entries_only`
- `long_trade_runner.enabled` + `trend_retention_giveback_ratio`
- `dual_regime.trend_retention` + `profit_run` (UPL≥£15 breathe)

Ranked rotator only expands **which markets** may enter; it does not re-open Instant/micro.

## Cutover checklist (operator)

1. Books **FLAT** (`/api/positions/live` verdict FLAT on `:8080` and `:8081`).
2. Confirm ranked knobs in `config/config_v31_demo_throughput.json` (+ overlay consistency — do not re-exclude Gold/EURUSD/FTSE).
3. Dual reload via anti-zombie / `v32_runtime_start.sh stop|start` (never `kill -9`).
4. Post-start: CFD `/api/stop`; SB `/api/start`.
5. Confirm `rotation.ranked_rotator` on `/api/rotation_state` — `mode=ranked`, `promoted` top-N, `dominant` matches Desk Intent focus.
6. GUI (Quantum Terminal `:3000`, UI-only): Desk Intent — SB **ARMED** (macro/LTR), CFD **PAUSED** (A2); **Ranked rotator ON** chip; focus = dominant; **Promoted** shows top-N (not DOW-only); rotation line `ranked · dominant … · promoted … · wait …`. AI Market Scanner labels promoted / eligible / waiting; Nikkei remains excluded. Hard-refresh after `terminal` rebuild — do not restart trading agents for GUI-only changes.

## Code

| Piece | Path |
|---|---|
| Rank + allowlist merge | `src/runtime/rotation_failover.py` |
| SB allowlist hook | `dual_core_execution._sb_hot_path_allowlist` |
| Selectivity / ElasticGate carve-out | `overnight_entry_policy` |
| Sweep tick + Desk Intent surface | `evaluate_multi_source_rotation_sweep` |
| Tests | `tests/test_rotation_failover.py` |

## Not in scope

- Forex channel-health failover (`FOREX_FAILOVER`) — separate path
- Auto-enabling Instant / micro / ElasticGate loosening
- CFD entries while A2 paused
- Nikkei hot-path until JPY PnL certified
