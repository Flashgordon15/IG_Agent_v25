# API Usage Map

Audit of external APIs in IG Agent v31. **Signal path = Yahoo/Finnhub/Alpha; IG = execution only.**

## Yahoo Finance

| Location | Data | Usage | Correct? |
|----------|------|-------|----------|
| `feeder/yahoo_quote_poller.py` | Live bid/offer | Primary hub publish, Gate 3 stream-ready | Yes |
| `data/ohlc_yahoo_seeder.py` | OHLC bars | ML cache, SignalEngine bootstrap | Yes |
| `system/feeds/multi_feed_hub.py` | (racing) | Backup alongside Finnhub | Yes |
| `trading/multi_api_broker.py` | Mid price | Apex API racing (timeout 1.5s) | Yes |
| `intelligence/telemetry_daemon.py` | Reference quotes | Gasket / telemetry overlay | Yes |

## Finnhub / Twelve Data

| Location | Data | Usage | Correct? |
|----------|------|-------|----------|
| `system/feeds/multi_feed_hub.py` | WebSocket ticks | First-past-the-post race → hub + alpha ring | Yes |

## Alpha Vantage

| Location | Data | Usage | Correct? |
|----------|------|-------|----------|
| `trading/multi_api_broker.py` | Global quote | Apex racing fallback | Yes |
| `scripts/ingest_external_data.py` | Historical | Offline ingest | Yes |

## IG REST / Lightstreamer

| Location | Data | Usage | Correct? |
|----------|------|-------|----------|
| `execution/live_executor.py` | Orders, deals | Trade submission | Yes |
| `ig_api/rest_client.py` | Positions, margin | Account sync, lifecycle | Yes |
| `runtime/ig_position_sync.py` | Open positions | Reconciliation | Yes |
| `feeder/execution_quote_preflight.py` | Snapshot | Pre-order spread check when `ig_snapshot_at_execution=true` | Yes (execution) |
| `system/market_data_hub.fetch_if_stale` | Live prices | **Blocked** when Yahoo primary | Fixed |
| `trading/ohlc_bootstrap.py` | Price history | **Skipped** when Yahoo primary | Fixed |
| `system/fast_stream_hydration.py` | Live prices | Gate 5 hub wait; **IG REST blocked** when Yahoo primary | Fixed |
| `ig_api/streaming_client.py` | Stream ticks | Legacy IG transport when `reference_transport=ig` | Legacy only |

### Blocked on signal path (Yahoo-primary)

| Call site | Previous role | v31 behaviour |
|-----------|---------------|---------------|
| `fast_stream_hydration._inject_rest_quotes` | IG GET /markets at G5 | Skipped — orchestrator wait |
| `market_data_hub.publish(source=rest)` | Initial hub seed | Rejected on night-matrix epics |
| Gate 5 `LIVE_FALLBACK` | IG quotes for boot | Not used when Yahoo primary |

## Internal / Other

| API | Location | Role |
|-----|----------|------|
| Telegram | `notifications/` | Operator alerts |
| Yahoo (no key) | Public chart API | Unauthenticated quotes |

## Conflicts resolved

1. **Duplicate Yahoo pollers** — Orchestrator owns Yahoo; multi-feed hub skips Yahoo race when poller active.
2. **8ms Yahoo timeout** in `multi_api_broker.py` — raised to 1.5s.
3. **IG overwriting Yahoo** in hub — publish guard + initial-seed block on signal path.
4. **IG OHLC on ML path** — Yahoo-first bootstrap in `ohlc_bootstrap.py`
5. **`ig_snapshot_at_execution: true`** — disabled in v31 config for signal isolation
6. **Gate 5 LIVE_FALLBACK** — IG REST hydration disabled in Yahoo-primary mode (`fast_stream_hydration.py`).
7. **Yahoo 429 storms** — exponential backoff in `yahoo_quote_poller.py`; staggered epic bootstrap.

## Rate limits

- IG REST: `RestApiBudget` 3 calls/min hard cap (execution + account sync only on signal path)
- Yahoo: poll interval from config (default 2–3s); **429 backoff** 8–120s; epic stagger 0.15–0.35s
- Finnhub/Twelve: WebSocket with reconnect in `multi_feed_hub`
