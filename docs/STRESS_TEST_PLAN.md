# IG Agent v25 — stress test plan

Operational proof before / during Tokyo session. Not a profitability test.

## 1. Unit / integration (local, ~2 min)

```bash
cd /path/to/IG_Agent_v25
PYTHONPATH=src python3 -m pytest \
  tests/test_execution_pipeline_e2e.py \
  tests/test_orchestrator_post_bootstrap_gates.py \
  tests/test_ohlc_bootstrap.py \
  tests/test_environment_scorer.py \
  tests/test_live_executor_confirm_inflight.py \
  tests/test_session_flatten_stress.py \
  -q
```

## 2. Mock pipeline (wiring only)

```bash
PYTHONPATH=src python3 scripts/e2e_execution_probe.py --mock-only
```

## 3. LIVE DEMO infra (no order)

```bash
PYTHONPATH=src python3 scripts/e2e_execution_probe.py
```

## 4. Agent soak (stability)

```bash
SOAK_DURATION_SEC=600 ./scripts/soak_test.sh
```

Watch: no repeating `insufficient bars` after OHLC bootstrap; ERRORS pill shows `env_scorer_fallback` if scorer falls back.

## 5. Runtime gate watch (session open)

Poll `http://127.0.0.1:8080/state` every 5 min for ~30 min after restart.

| Check | Pass |
|-------|------|
| OHLC log | `injected 100 bars ... (market=Japan 225)` |
| Scorer | No repeating `insufficient bars` |
| Gates 1–5 | Pass when market open |
| Gate 6 | Past `collecting candle history` (may still block on RSI/threshold) |
| Gate 7 | `armed` when 1–6 pass |

## 6. Session-end flatten (controlled)

**Automated (no IG):**

```bash
PYTHONPATH=src python3 -m pytest tests/test_session_flatten_stress.py -q
```

Simulates: mock open position → T-5 flatten → `FLATTEN CONFIRMED` in engine log.

**Manual (maintenance window or near session close):**

1. Start agent with DEMO credentials.
2. Open a small DEMO position on Japan 225 manually in IG (or leave agent-opened position).
3. Within 5 minutes of session end, confirm engine log shows:
   - `session flatten — closing all open positions (T-5min)`
   - `FLATTEN CONFIRMED — all positions closed`
4. Dashboard should show 0 open positions after sync.

Do not lower `signal_threshold` or relax RSI without replay analysis (`scripts/replay_signals.py`).
