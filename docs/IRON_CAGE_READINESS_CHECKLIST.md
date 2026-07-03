# Iron Cage Readiness Checklist

The **Iron Cage** is the strict readiness contract for IG Agent v31. The GUI must not show “Ready to trade” until every layer below is genuinely operational.

## Boot gates (G1–G5)

| Gate | Stage | Pass criteria |
|------|-------|---------------|
| G1 | Core init | Config loaded, logging, basic services |
| G2 | Auth + metadata | IG auth OK, instrument metadata hydrated |
| G3 | Data feeds | DataFeedOrchestrator running, Yahoo bootstrap started |
| G4 | Routing | Unified routes armed, strategy warm-up |
| G5 | Execution | Dual-core sweep alive, lifecycle engine ready |

**Checks**

- [ ] `GET /api/boot_status` — all stages green or `iron_cage.gates` all `complete`
- [ ] No gate wedged >120s (`gate_watchdog` heal active)
- [ ] `/api/boot_log` shows progression without silent stalls

## Data feeds

**Hierarchy:** Yahoo (primary) → Finnhub/Twelve (fallback race) → **never IG on signal path**

- [ ] `GET /api/data_feed_state` — `health=ok`, `fresh_count >= 1`, `primary_feed=yahoo`
- [ ] `ig_on_signal_path=false`
- [ ] `retry_backoff_sec=0` (or documented Yahoo 429 cooldown)
- [ ] Hub not starved (`feed_starvation` absent from blockers)

## Execution & routing

- [ ] `GET /api/health_light` — `execution_loop_active=true`, `stacked_sweep_alive=true`
- [ ] `routing_state.armed > 0`
- [ ] `GET /api/rotation_state` responds <1s

## IG (execution only)

- [ ] `GET /api/ig_budget_state` — `rate_limited=false`, `execution_paused=false`
- [ ] REST budget not in cooldown
- [ ] Orders use IG REST only; ML/signals use data APIs (see `docs/API_USAGE_MAP.md`)

## Readiness contract

Authoritative evaluator: `src/system/iron_cage_readiness.py`

- [ ] `GET /api/iron_cage_status` — `trade_ready=true`
- [ ] `GET /api/health` — `ok=true`, `trade_ready=true`, no `iron_cage:*` issues
- [ ] Cockpit **Ready to trade** pill green

### Blockers (any one forces `trade_ready=false`)

- `gates_incomplete`, `boot_not_ready`
- `feed_starvation`, `ig_on_signal_path`, `no_primary_feed`
- `execution_inactive`, `routing_unarmed`
- `ig_rate_limited`

## GUI cockpit

- [ ] Iron Cage banner shows G1–G5 gate pills
- [ ] Feed / exec / IG budget lines update live (1s poll)
- [ ] Subsystem matrix badges match `/api/iron_cage_status`
- [ ] No flicker on broker-ready matrix (2s poll)

## Automated verification

```bash
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_iron_cage_readiness.py tests/test_data_feed_orchestrator.py -q
PYTHONPATH=src python3 scripts/data_feed_diagnostic.py
curl -s http://127.0.0.1:8080/api/iron_cage_status | python3 -m json.tool
./scripts/boot_acceptance.sh
```

## Related docs

- `docs/DATA_FEED_HIERARCHY.md` — feed priority model
- `docs/API_USAGE_MAP.md` — which API for which purpose
- `docs/TRADING_LOGIC_OVERVIEW.md` — signal → execution pipeline
