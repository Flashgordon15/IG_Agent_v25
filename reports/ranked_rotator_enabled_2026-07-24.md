# Ranked Multi-Market Rotator — Enabled 2026-07-24

**Cutover:** anti-zombie dual recycle (books FLAT) after sibling Gold-only enable  
**Supersedes:** Gold-only DOW-stale failover escape hatch  
**Config:** `config/config_v31_demo_throughput.json` + `config/tuning_overlay.json`

## Live PIDs (post-cutover)

| Engine | Port | Account | Origin | PID | Entries |
|---|---|---|---|---|---|
| CFD Sniper | :8080 | Z6BAH4 | QUANT_SNIPER | **9913** | **PAUSED** (A2 `/api/stop`) |
| SB Sentinel | :8081 | Z6BAH3 | MACRO_SENTINEL | **10190** | **ARMED** (`/api/start`) |

## What can trade (SB)

Ranked candidates (compete every sweep):

| Epic | Label | Size floor | Notes |
|---|---|---|---|
| `IX.D.DOW.IFM.IP` | DOW | 0.5 £/pt | Not permanently privileged |
| `CS.D.CFPGOLD.CFP.IP` | Gold | 10 £/pt | |
| `CS.D.EURUSD.CFD.IP` | EUR/USD | 1 £/pt | SB wire → `CS.D.EURUSD.TODAY.IP` |
| `IX.D.FTSE.IFM.IP` | FTSE | 0.5 £/pt | |

**Excluded from hot path:** Nikkei (JPY PnL not certified), GBPUSD, DAX, Crude.

**Entry style on SB:** macro / LTR / Trend-Retention only — Instant + Core-B micro HARD OFF.  
**CFD:** A2 entries paused (no Instant re-enable).

## How rank works

1. Score each candidate = rotation composite + eligible bonus (+12) + optional journal expectancy tilt (±15).
2. Promote top `ranked_promote_top_n` (**2**) onto the **effective** SB allowlist (replaces static DOW-only base).
3. Dominant = rank #1 → routing `current_epic` + Desk Intent focus.
4. Weaker markets (including DOW) demote when outside top-N.
5. Selectivity `non_dow` carve-out applies only to promoted epics.

Observed shortly after cutover (`/api/rotation_state` on :8081):

- `mode`: `ranked`
- `dominant`: DOW (tied composite with Gold; DOW listed first on equal score)
- `promoted`: DOW, Gold
- EURUSD rank 3 (eligible), FTSE rank 4 (not eligible this sweep)

## LTR / trail armed (unchanged, verified on disk)

- `dual_regime.sb_disable_instant_micro` / `sb_disable_core_b_micro` / `sb_macro_ltr_entries_only` = true
- `long_trade_runner.enabled` = true + Trend-Retention giveback
- `profit_run` / `trend_retention` UPL≥£15 breathe path

## What to watch on GUI (Desk Intent)

1. **Engines:** CFD = PAUSED · “A2 · entries paused”; SB = ARMED · “macro/LTR”.
2. **Focus / rotation:** should follow `ranked_rotator.dominant` (label `ranked · {MARKET}` after terminal rebuild).
3. **On open:** GBP exit + virtual stop + dynamic trail / LTR giveback — trail witnessing on longer SB holds.
4. **API SoT:** `GET :8081/api/rotation_state` → `rotation.ranked_rotator`.

## Tests

```text
tests/test_rotation_failover.py — 10 passed
```

Coverage: ranked promote/demote, Nikkei never promoted, SB micro still vetoed, legacy DOW-stale path retained when `ranked_rotator_mode:false`, config candidates include DOW/Gold/EURUSD/FTSE.

## Hard constraints preserved

- No `kill -9`; anti-zombie stop/start only while FLAT
- Instant/micro not re-enabled
- Hard-cap 1, size floors, overnight bans, OBI fail-closed
- Nikkei remains on `exclude_from_hot_path`
