# IG Agent v29.1 — System Health Specification

Authoritative reference for **what good looks like** at boot, runtime, and weekly cycle boundaries.
Aligned with production Mac Mini deployment (`streaming_transport: rest_poll`).

**Version baseline:** `3fa03f4` and later (quote burst `e272389`, health alignment `50d6b25`).

---

## 1. Boot Sequence

Ordered milestones with expected log signatures. Times are wall-clock from process start.

| Window | Requirement | Expected log / signal |
|--------|-------------|------------------------|
| **≤10s** | PID registered; instance lock matches `:8080` listener; `manual_stop.json` absent | `Instance lock acquired pid=XXXXX` |
| **≤30s** | IG REST authenticated; session token valid | IG auth / session established (REST client ready) |
| **≤45s** | Price transport active for all **6 enabled epics** | `market stream started epics=[...] transport=REST poll` (Mac Mini uses **REST poll**, not Lightstreamer) |
| **≤60s** | All 6 trading loops running; gate activity recorded | `trading_loop started epic=...` (×6); `record_gate_evaluation` within 120s |
| **≤90s** | INITIALIZING cleared; `trading_healthy` → true; all 6 enabled epics `LIVE` | `init_force_cleared=true` OR natural clear; `[HEALTH] trading_healthy = true` |
| **≤120s** | Hub quote age &lt; 30s on enabled epics; rotation logged once; REST headroom &gt; 50% | `[ROTATION RANK] ...`; `rest_budget_pct` ≥ 50 |

### Stale lock handling (before restart)

| Condition | Action |
|-----------|--------|
| `.ig_agent_v29.lock` exists, PID **dead** | Remove lock; proceed with boot |
| Lock exists, PID **alive** on `:8080` | **Do not** start second instance |
| Lock cleared | Log: `[BOOT] Lock cleared — stale PID XXXXX` (via `acquire_instance_lock` reclaim) |

Lock file: `src/data/.ig_agent_v29.lock`

---

## 2. Expected Market States

### LIVE — 6 enabled epics (always LIVE when agent healthy)

| Epic | Label |
|------|-------|
| `CS.D.CFPGOLD.CFP.IP` | Gold |
| `CS.D.EURUSD.CFD.IP` | EUR/USD |
| `CS.D.GBPUSD.CFD.IP` | GBP/USD |
| `IX.D.NIKKEI.IFM.IP` | Japan 225 |
| `IX.D.DOW.IFM.IP` | Wall Street |
| `IX.D.NASDAQ.IFM.IP` | US Tech 100 |

### DISCONNECTED — rotation universe placeholders (correct, not a bug)

- Additional epics in `GLOBAL_ROTATION_UNIVERSE` appear in `/state` as `DISCONNECTED`.
- **Health checks use `_configured_epics()` (loop epics only)** — rotation pool must **not** fail health.

### CLOSED — session-dependent

| Instrument | CLOSED window (Europe/London) |
|------------|-------------------------------|
| Gold | Fri 20:00 → Mon 06:00; daily 21:00–06:00 weekday overnight |
| Nikkei | Outside `asia_early` (typ. 00:00–06:00 Mon–Fri) |
| DOW / NASDAQ | Outside US session (typ. 13:30–22:00 Mon–Fri) |
| EUR/USD, GBP/USD | Near 24/5; CLOSED weekend wrap per IG calendar |

---

## 3. REST Rate Limit Health

| Parameter | Value |
|-----------|-------|
| Hard cap | **3** non-essential calls / minute |
| Quote polls | **Exempt** from hard cap (`stream_quote_poll_rest_window`, `e272389`) |
| Burst spacing | **200ms** between market calls inside poll window |
| Safe headroom | `rest_budget_pct` **> 50** |
| Warning | `rest_budget_pct` **< 25** → log `[REST WARNING]` (budget metrics) |
| Critical | `rest_budget_pct` **< 10** → non-essential calls deferred |
| IG rate-limit reset | ~60 minutes (rate limit manager) |

When IG rate-limit blocks at restart:

- Quote polls continue (exempt).
- Defer history, account refresh, non-essential sync.
- Log: `[REST LIMIT] Budget low — deferring non-essential calls`

### REST poll stall (transport)

| Signal | Threshold |
|--------|-----------|
| `rest_poll_stalled` | `true` if no successful poll tick for **≥30s** |
| Telegram | After **3** consecutive stall cycles |
| Recovery log | `[REST POLL] Recovered after Xs stall` |

---

## 4. Healthy Runtime State

### Timing

| Metric | Target |
|--------|--------|
| `loop_tick` | &lt; 5s per instrument |
| `probe_gate_evaluation` p50 | &lt; 10ms |
| `probe_gate_evaluation` p95 | &lt; 500ms |
| `probe_snapshot_publish` p50 | &lt; 2000ms |
| Hub quote age (enabled epics) | &lt; 30s |
| `rest_budget_pct` | &gt; 25 |

### State

| Field | Healthy value |
|-------|---------------|
| `trading_healthy` | `true` |
| `manual_stop.json` | absent |
| Instance lock | matches `:8080` PID |
| Drawdown shield | TRADING ALLOWED |
| Enabled epics `stream_status` | `LIVE` |
| BST clock (`/api/time`) | within 5s of system |
| Logs | no `[FLATTEN ABANDONED]` |
| Issues | no `quotes_stale` on enabled epics |

### Health vs dashboard alignment (`50d6b25`)

- `/api/health` and `/state` both derive quote freshness from **`hub_quote_stream_tick_age`**.
- If health reports stale but dashboard shows `LIVE` for same epic → **health logic is wrong** (regression).

### `trading_healthy = true` requires ALL of:

1. `trading_loops_running` and not paused  
2. `last_gate_check_age_sec` ≤ 120 (not null)  
3. No actionable `quotes_stale` on **configured epics only** (open markets with hub age ≤ `health_quote_max_age_rest_poll_sec`, default 120s)  
4. `watchdog_active` and `supervision_drift_ok` for top-level `ok` (launchd on Mini)

**Maximum warmup:** **90 seconds** after live quotes for INITIALIZING clear.  
**If still false after 90s** with 6/6 `LIVE` and fresh hub ages:

1. Run `./scripts/health_check.sh`
2. Inspect `issues[]` on `/api/health`
3. Check `gate_age`, `watchdog_active`, IG rate-limit block
4. Do **not** count rotation-pool `DISCONNECTED` epics

---

## 5. Healthy Trade Lifecycle

### Entry

1. Signal on **closed** 5m bar (`iloc[-2]`)
2. Gate stack (12 gates, see §6)
3. Interim scorer logs 4 components + total
4. Route to IG REST (not simulator unless `dry_run=1`)
5. IG confirm ≤ 10s
6. Stop attached ≤ 5s after confirm
7. Position in runtime within 1 tick
8. DB row with `ig_deal_id`
9. ML jsonl row appended

### During trade

- Trailing stop ~50ms fast track
- Stale decay after 15 min without MFE progress
- PATCH-003 at Rung 1 (1.5R) and Rung 2 (2.5R)
- Stop never widens

### Exit

- IG close confirm ≤ 10s
- DB: `exit_price`, `ig_pnl_currency`
- ML row updated; points engine; 10 min cooldown on epic

---

## 6. Gate Stack Order

Pre-gate: `active_rotation` (not in `GATE_NAMES`)

| # | Gate |
|---|------|
| 1 | `session_open` |
| 2 | `session_blackout` |
| 3 | `cold_start_gap` |
| 4 | `environment_fitness` |
| 5 | `points_state` |
| 6 | `correlation_ok` |
| 7 | `risk_validation` |
| 8 | `expectancy_ok` |
| 9 | `calendar_ok` |
| 10 | `signal_confidence` |
| 11 | `ml_veto` |
| 12 | `execution` |

Signal engine pre-blocks (before ML): `session_blackout` → `economic_calendar` → `reentry_cooldown` → `session_trade_cap`.

---

## 7. Weekly Cycle (Europe/London)

| Time | Event |
|------|-------|
| Mon 06:00 | London open — FX, Gold active |
| Fri 19:00 | Friday flatten arms — monitoring |
| Fri 19:30 | Friday flatten fires — close all |
| Fri 19:45 | Flatten confirm — 0 IG positions |
| Sat–Sun 21:59 | No new entries (blackouts) |
| Sun 22:00 | Nikkei / FX resume |

---

## 8. Verification Commands

```bash
./scripts/health_check.sh
PYTHONPATH=src .venv/bin/python3 -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python3 -m pytest tests/ -q
```

**Golden path tests:** `tests/test_golden_path.py`
