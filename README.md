# IG Agent v29.1

Automated IG CFD trading agent — Python backend (FastAPI + multi-market trading loop) on `localhost:8080`, React dashboard in `dashboard/`.

**Authoritative docs:**

| Document | Purpose |
|----------|---------|
| [`IG_Agent_v29.1_COMPLETE_SPEC.md`](IG_Agent_v29.1_COMPLETE_SPEC.md) | Full operator + implementer specification |
| [`docs/V29.1_ARCHITECTURE.md`](docs/V29.1_ARCHITECTURE.md) | Module map, data flow, diagrams |
| [`IG_Agent_v25_COMPLETE_SPEC_v8.md`](IG_Agent_v25_COMPLETE_SPEC_v8.md) | Historical v25.5 reference |
| [`IG_Agent_v26_FRAMEWORK.md`](IG_Agent_v26_FRAMEWORK.md) | Future multi-strategy vision |

## Running (single command)

```bash
# From repo root — trading + dashboard
PYTHONPATH=src python3 src/main.py

# Browser
open http://localhost:8080

# Rebuild dashboard after UI changes
cd dashboard && npm run build
```

**macOS:** use Desktop launcher `IG Agent v29.0.app` (runs same entry point).

## Configuration

- **Primary overlay:** `config/config_v29.json` (v29.1 protective learning, demo mode)
- **Instrument matrix:** `config/config_v25.json`
- Credentials: interactive at startup (not persisted to disk)

## Quick health checks

```bash
PYTHONPATH=src python3 scripts/learning_health_report.py
PYTHONPATH=src python3 -m pytest tests/ -q
```

Restart the agent after Python or config changes.

---

## Version 29.1 Changelog Summary

**June 2026 — Flight Deck & broker-sync hardening**

1. **Decimal-safe balance parsing** — IG account summary and P&L paths coerce `Decimal` → `float` before JSON/WebSocket serialization; `sanitize_for_ws_json` guards all Flight Deck frames against NaN/inf leakage.

2. **Split Active / Closed UI grid** — Card D separates open-position telemetry (floating P&L, side badges) from a dedicated 24h closed-transaction history table.

3. **Broker-linked history hydration** — `IGRestClient.fetch_transaction_history()` pulls `/history/transactions`; `web_server` merges IG ledger rows with local session logs via `merge_closed_trades` and normalizes through `_normalize_closed_trade_row_for_ui`.

4. **Isolated multi-market HUD gauges** — Card B reads per-asset slices (`GOLD`, `WALL_STREET`, `JAPAN_225`, `EUR_USD`) from `snapshot_store.get_tick().markets` via `cockpit.avionics_markets.enrich_avionics_markets`; confidence/RSI bars, BLOCKER, and AI WEIGHT labels are scoped per card container (no global `micro_confidence` bleed).
