# Trading Logic Overview

End-to-end pipeline for IG Agent v31 — from market data to lifecycle exit.

## 1. Data ingestion

```
Yahoo poller ──┐
Finnhub WS  ───┼──► DataFeedOrchestrator ──► MarketDataHub.publish()
Twelve WS   ───┘         │                        │
                         │                        ▼
                    /api/data_feed_state      Trading loops (tick)
```

- **Primary:** Yahoo reference quotes (`feeder/yahoo_quote_poller.py`).
- **Backup:** Finnhub / Twelve Data racing hub (`system/feeds/multi_feed_hub.py`).
- **OHLC:** Yahoo seeder → local cache → `SignalEngine.seed_ohlc_history` (`trading/ohlc_bootstrap.py`).
- **IG:** Session auth (Gate 2), orders, positions only.

Boot wiring: Gate 3 → orchestrator → stream-ready; post-ready ensures orchestrator idempotent.

## 2. ML / environment scoring

- `EnvironmentScorer` consumes OHLC + macro context.
- Learning plane reads closed trades from SQLite; ML blend at ≥500 records.
- Protective learning floors (62% conf / 55 fitness) when enabled.

## 3. Signal generation

Per-epic `TradingLoop` thread (`runtime/market_orchestrator.py`):

1. Hub quote (Yahoo-sourced, max 45s age).
2. `SignalEngine.evaluate()` — indicators, confidence, fitness.
3. Shadow log on all return paths.

## 4. Decision layer

- `PointsEngine` — entry scoring.
- `RiskManager` — size, daily loss, REST budget.
- `EntryProtection` — session locks, rollover 21:58–22:05 BST, night matrix 24/7.
- Dual-core rotation (`runtime/dual_core_execution.py`) — stack Wall St + Gold, sweep universe by tick velocity / z-score.

## 5. Pre-trade validation

- Quote trust guard (`open_position_view` / quote age).
- Broker reject guard, correlation guard.
- Execution quote preflight (optional IG snapshot at order time only).
- `trade_ready` boot contract requires healthy primary feed.

## 6. Execution

- `LiveExecutor` / v31 order router → IG REST deal submission.
- Demo/LIVE routing via credentials + `operating_mode`.
- In-flight timeout 30s; REST budget enforced.

## 7. Lifecycle management

- `TradeManager` / `trade_lifecycle` — open → trailing → exit.
- IG position sync reconciles broker vs ledger.
- Virtual stops + dynamic limit engine for limit updates.
- Triage ledger for rejections and diagnostics.

## 8. Trailing / dynamic limits

- Config: `trailing_stop_distance` (v31 default 45 pts).
- `dynamic_limit_engine` adjusts limits from volatility / structure.
- Hub quotes for **monitoring** come from Yahoo; IG used to **confirm** fill and modify broker stops.

## 9. Exit logic

- Signal-driven exits + protective stops.
- Rollover flatten rules.
- Manual flatten via `/api/flatten/*` cockpit controls.

## Cockpit visibility

| Endpoint | Purpose |
|----------|---------|
| `/api/data_feed_state` | Feed health, primary, fallback |
| `/api/v31/telemetry` | Ticks, z-score, dual-core status |
| `/api/rotation_state` | Multi-market rotation + feed overlay |
| `/api/health` | Boot gates, block reasons |

## Multi-market instruments

Night matrix (24/7): Gold, Wall St, Nikkei, EUR/USD (+ Crude, FTSE, DAX in hub universe).

Rotation universe drives stacked tracks; Yahoo epic mapping in `data/ohlc_yahoo_seeder.EPIC_YAHOO_MAP`.
