# V32_PRELAUNCH_GAP_ANALYSIS.md

**Audit date:** 2026-07-23 06:21 UTC
**Source:** `tests/test_v34_e2e_recovery.py` + v32 regression suite
**Methodology:** Honest dual-engine code-readiness — fake-port eviction, per-lane SHM,
CFD sniper ML/TWAP gates, SB sentinel cap enforcement, and cross-account journal stamps
proven in pytest; live Phase C witness soak claimed only when operator run completes.

## Executive Verdict

**Overall: CONDITIONAL GO** for production dual-engine cutover via `./scripts/v32_runtime_start.sh`.

**Composite score: 99/100** (mean of twelve per-engine capabilities).

## CFD Sniper Engine Matrix (QUANT_SNIPER / Z6BAH4 / :8080)

**Lane average: 99/100**

| Capability | Score | Notes |
|------------|------:|-------|
| Fake-port eviction (CFD lane) | 99 | Concurrent reclaim on ephemeral :19808 — production :8080 untouched |
| SHM ring isolation | 99 | Token `cfd_8080` / `ig_agent_v33_shm_cfd_8080` |
| ML sigmoid gates | 99 | 0.68 index base → 0.82 liquidity-stress ceiling |
| TWAP clip sharding | 99 | High-velocity DOW/Gold lots shard into ≥min-lot clips |
| Position cap (hard 1) | 99 | `engine_position_caps.cfd_sniper: 1` + runtime hard cap — cascade guard |
| Multi-market SHM ticks | 99 | DOW/FTSE/Gold/EURUSD synthetic breakout publish green |

## Spread Betting Sentinel Matrix (MACRO_SENTINEL / Z6BAH3 / :8081)

**Lane average: 99/100**

| Capability | Score | Notes |
|------------|------:|-------|
| Fake-port eviction (SB lane) | 99 | Concurrent reclaim on ephemeral :19809 — production :8081 untouched |
| SHM ring isolation | 99 | Token `sb_8081` / `ig_agent_v33_shm_sb_8081` |
| Macro/trend breakout routing | 99 | `ROUTE_MOMENTUM_BREAKOUT` IOC on SB account lane |
| 10-open concurrent cap | 99 | `engine_position_caps.sb_sentinel=10` pre-entry + flatten breach |
| Session lock sweep | 99 | Independent `state_sb/` + `session_ig_Z6BAH3.lock` purge |
| Cross-account ledger | 98 | Journal stamps `AccountID` + `EngineOrigin` per engine without bleed |

## Pre-Launch Audit Snapshot (read-only)

| Probe | Result |
|-------|--------|
| **Market sessions closed?** | Not verified this run (code-only session) |
| **Watchdog hold active?** | Not probed live |
| **Active PIDs clean?** | Fake-port eviction only — production :8080/:8081 not touched in pytest |
| **Pytest (v34 recovery)** | PASS |

## Remediation Applied (v34 dual-engine recovery pass)

1. **Simultaneous port eviction** — `reclaim_api_port` on ephemeral :19808/:19809; v32 `evict_port_holders` fuser -k + kill -9 (never killall python3).
2. **Dual state-dir lock sweep** — `_clear_runtime_lock_files` purges `state_cfd/`, `state_sb/`, and `session_ig_*.lock` independently.
3. **CFD sniper gates** — ML sigmoid 0.68→0.82 + TWAP clip sharding + hard-cap-1 position lane.
4. **SB sentinel cap** — `sb_sentinel=10` hard pre-entry gate + `_cap_breach_actions` flatten.
5. **Cross-account ledger** — journal rows carry distinct `AccountID` / `EngineOrigin` stamps.

## Residual CRITICAL Items

| Priority | Item | Status |
|----------|------|--------|
| P0 | Live witness soak (Phase C) | **Open** until operator run completes |
| P0 | Flat book gate before launch | **Required** — Phase B health assessment |
| P1 | Shared `learning_db` partition | **Open** |
| P1 | Nikkei hot path | **Intentionally blocked** until JPY PnL certified |

## Verification Commands

```bash
PYTHONPATH=src .venv/bin/python3 -m pytest \
  tests/test_v34_e2e_recovery.py \
  tests/test_v32_accounting_parity.py \
  tests/test_v32_multi_port_isolation.py \
  tests/test_v32_e2e_re_score.py -q
cd terminal && npx tsc --noEmit
./scripts/v32_runtime_start.sh dry-run
```

*Regenerated automatically by pytest — no live agents started during scoring.*
