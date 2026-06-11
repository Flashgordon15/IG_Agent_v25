# IG Agent v25 — Claude Code Context

## Current State
- Branch: feature/v25-ml-enhancement
- Account: Z6BAH4 (DEMO CFD)
- Markets: 6 active epics (Japan 225, EUR/USD, GBP/USD, US Oil, Wall Street, Gold)
- Points: CAUTION, cumulative=3.0
- Status: OPERATIONAL

## Fixes Made (03/06/2026)

### Original 5 root cause fixes (from earlier session):
1. **REST budget deadlock** — 30s order-in-flight timeout (`src/system/rest_api_budget.py`)
2. **REST lock reservation pattern** — prevents concurrent REST calls (`src/system/rest_api_budget.py`)
3. **FRESH_STREAM_TICK_MAX_AGE_SEC raised to 45s** — (`src/ig_api/lightstreamer_streaming.py` / config)
4. **quote_source() uses hub.get_snapshot() only** — (`src/trading/trading_loop.py`)
5. **SPREAD_NORMAL_MULTIPLIER raised to 2.5x** — (`src/trading/trading_loop.py`)

### Fixes made in this session (03/06/2026):
6. **Telegram pts_before NameError** — `_telegram_trade_closed()` used `pts_before` instead of `points_before` param — fixed in `src/trading/trade_manager.py:396`
7. **Time Open column** — added `_open_mins_for_deal()` to `IgPositionSync.snapshot_dict()` + `_compute_open_mins()` in `open_position_view.py`
8. **one_position_per_epic enabled** — `config/config_v25.json` (prevents stacking same epic)
9. **New markets added** — GBP/USD, US Oil WTI, Wall Street Dow (config + OHLC seeded)
10. **Session whitelists updated** — Japan 225 gets `asia_late`; EUR/USD gets `us_afternoon`
11. **Dashboard safety controls** — Close All, per-position close buttons, Stop/Restart Agent, health panel
12. **API endpoints added** — `/api/flatten/all`, `/api/flatten/{epic}`, `/api/agent/stop`, `/api/agent/restart`
13. **Telegram message formats** — Updated to spec: 📈 open, ✅/❌ close, 🟢 startup, 🔴 stop, 🚨 critical
14. **OHLC Yahoo seeder** — Added GBP/USD, US Oil, Wall Street mappings to `src/data/ohlc_yahoo_seeder.py`

## Key Config Values (`config/config_v25.json`)

| Market | Epic | Threshold | Sessions | Risk Cap |
|--------|------|-----------|----------|----------|
| Japan 225 | IX.D.NIKKEI.IFM.IP | 70% | asia_early, asia_late | £50 |
| EUR/USD | CS.D.EURUSD.CFD.IP | 70% | london_morning, london_us_overlap, us_afternoon | £10 |
| GBP/USD | CS.D.GBPUSD.CFD.IP | 88% | london_morning, london_us_overlap, us_afternoon | £10 |
| US Oil | CS.D.CRUDE.CFD.IP | 90% | london_us_overlap, us_afternoon | £150 |
| Wall Street | IX.D.DOW.IFM.IP | 90% | london_us_overlap, us_afternoon | £50 |
| Gold | CS.D.CFPGOLD.CFP.IP | 70% | london_morning, london_us_overlap, us_afternoon | £150 |

- `FRESH_STREAM_TICK_MAX_AGE_SEC`: 45
- `SPREAD_NORMAL_MULTIPLIER`: 2.5
- `max_open_positions`: 3
- `one_position_per_epic`: true

## Architecture Decisions
- `quote_source()` uses `hub.get_snapshot()` only (no REST fallback for price quotes)
- Order-in-flight has 30s hard timeout to prevent REST budget deadlock
- Yahoo Finance OHLC fallback for all 6 markets (5m bars, 60-day history)
- Telegram chat_id: 1347145610 (config `telegram.chat_id`)
- Dashboard at localhost:8080
- Position sync every 15s when open positions, 30s when flat
- Duplicate position guard: `one_position_per_epic: true` + `epic_has_pending_open()` in executor

## Telegram Message Formats
- Trade open:  `📈 {market} {direction} at {price}\nSize:{size} Stop:{stop} Signal:{conf}%\nFitness:{fitness}% Points:{state}`
- Trade close WIN:  `✅ WIN {market} +£{pnl} +{pts}pts\nCumulative: {cumulative}pts {state}`
- Trade close LOSS: `❌ LOSS {market} -£{pnl} {pts}pts\nCumulative: {cumulative}pts {state}`
- Agent start: `🟢 IG Agent v25 started\nMarkets: {n} active | Points: {state}`
- Agent stop:  `🔴 IG Agent v25 stopped`
- Critical:    `🚨 CRITICAL: {message}`

## Dashboard Safety Controls
- **LIVE tab**: "CLOSE ALL POSITIONS" button (2-step confirm) → POST /api/flatten/all
- **TRADES tab**: Per-row "Close" button (confirm inline) → POST /api/close/{deal_id}
- **SYSTEM tab**: "STOP AGENT" (RED, type CONFIRM) → POST /api/agent/stop (flatten+stop)
- **SYSTEM tab**: "RESTART AGENT" (AMBER, type CONFIRM) → POST /api/agent/restart
- **SYSTEM tab**: Health panel (uptime, P&L, positions, points, markets)

## Known Remaining Items
- Phase 5 E2E trade test: set Gold threshold to 50 temporarily, verify full lifecycle
- `max_positions_per_epic: 3` in config conflicts with `one_position_per_epic: true` — new positions won't stack anyway, old multiple positions will still be tracked
- 3 Gold positions open from earlier session (opened before one_position_per_epic was enabled)

## How To Restart
```bash
pkill -f "src/main.py"
rm -f src/data/.ig_agent_v25.lock
caffeinate -i PYTHONPATH=src python3 src/main.py &
```

## How To Validate
```bash
PYTHONPATH=src python3 scripts/pre_flight_check.py --live
PYTHONPATH=src python3 scripts/e2e_platform_validation.py
```

## Session Windows (BST)
| Market | Hours |
|--------|-------|
| Japan 225 | 23:00-06:00 |
| EUR/USD | 07:00-17:00 |
| GBP/USD | 07:00-17:00 |
| Gold | 08:00-21:00 |
| US Oil | 13:00-21:00 |
| Wall Street | 13:30-21:00 |
| **Gap** | 21:00-23:00 BST (2 hours) |

## 24-Hour Coverage
```
23:00 ─────────────────────── Japan 225 ────────────────── 06:00
07:00 ──── EUR/USD + GBP/USD ──────── 17:00
08:00 ───────────── Gold ────────────────────────── 21:00
            13:00 ── US Oil + Wall Street ─────── 21:00
```

## Launch
Click **IG Agent v25** app icon on Desktop — it auto-detects if already running and just opens the browser if so. Uses caffeinate to prevent Mac sleep.
