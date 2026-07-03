# Data Feed Hierarchy

## Design principle: first past the post

| Tier | Providers | Role |
|------|-----------|------|
| **Primary** | Yahoo Finance | Reference bid/offer for signals, ML features, cockpit display |
| **Backup** | Finnhub, Twelve Data | Racing WebSocket feeds — first fresh tick wins per epic |
| **Tertiary** | Alpha Vantage | Apex multi-API broker racing / offline OHLC ingest |
| **Execution only** | IG REST / Lightstreamer | Orders, stops, limits, position confirmation — **never** signal path |

## Orchestrator

Module: `src/system/feeds/data_feed_orchestrator.py`

Boot sequence (non-blocking):

1. Start Yahoo poller for rotation universe + sync bootstrap (Wall St + Gold).
2. Arm multi-feed racer on a background thread (Finnhub / Twelve Data).
3. Retry loop every 15s for degraded providers without blocking Gate 3/4/5.

API: `GET /api/data_feed_state`

```json
{
  "health": "ok|degraded|offline",
  "primary_feed": "yahoo",
  "fallback_active": false,
  "fresh_count": 7,
  "feeds": { "yahoo": {...}, "finnhub": {...}, "twelve_data": {...} }
}
```

## Hub publish rules

`MarketDataHub.publish()` rejects IG REST ticks that would overwrite a fresh (&lt;45s) Yahoo/backup quote when `pricing.reference_transport=yahoo`.

`MarketDataHub.fetch_if_stale()` returns cache only on the signal path — no IG REST fallback in Yahoo mode.

## Trade readiness gate

`boot_orchestrator` sets `trade_ready=true` only when:

- `signal_feed_health_ok()` — at least one fresh **non-IG** quote on the signal path
- `primary_feed_active()` — named primary feed present (Yahoo or approved backup)
- `orchestrator_feed_ok` — orchestrator health not `offline`
- **No** `ig_used_for_signal_path()` — IG REST quotes must not populate the hub signal path
- Existing execution subsystems OK (routing armed, stacked sweep, IG session)

G5 `ready=true` may flip earlier; splash `trade_ready` follows Stage G contract above.

## Yahoo rate limiting

- `yahoo_quote_poller.py` detects HTTP **429** and applies exponential backoff (8s → 120s cap).
- Poller loop stretches interval during backoff; bootstrap sync staggers epics (`IG_YAHOO_BOOTSTRAP_EPIC_GAP_SEC`, default 0.35s).
- `multi_feed_hub` **skips** duplicate Yahoo race loop when orchestrator poller is active.

## IG execution-only enforcement

- `fast_stream_hydration.py` — **no** IG REST quote inject when `reference_transport=yahoo`; waits on orchestrator instead.
- `market_data_hub.publish()` — blocks **initial** IG REST seeds on night-matrix epics in Yahoo mode.
- Overwrite guard still protects fresh Yahoo/backup quotes from IG REST clobber.

## Configuration

`config/config_v31.json`:

```json
"pricing": {
  "reference_transport": "yahoo",
  "yahoo_poll_seconds": 2.0,
  "ig_snapshot_at_execution": false
}
```

## Diagnostics

```bash
PYTHONPATH=src python3 scripts/data_feed_diagnostic.py
```

Exit code 0 = healthy feeds and no IG quotes on signal path.
