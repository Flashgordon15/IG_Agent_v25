# IG Agent Core Platform: Strategic Enhancements Roadmap

Official architectural roadmap for quantitative upgrades on the v29.1 platform.

**Last updated:** 2026-06-17 · Phase 2 shipped on `main`

---

## Production complete (v29.1 + Phase 2 — June 2026)

| Status | Capability |
|--------|------------|
| ✓ | Sub-microsecond Trailing Stop Evaluation Engine (~1.5µs execution) |
| ✓ | Asynchronous Non-Blocking Broker Stop Dispatch Worker |
| ✓ | Real-time 3-Stage Boot Progress Bar & Password Firewall |
| ✓ | IG 0.5% Commercial FX Fee & Automated Min-Distance Clamps |
| ✓ | 2-Per-Epic Allocation Cap Loops Priority Harmonization |
| ✓ | **Flight Deck Co-Pilot HUD** (`:8787`) — master vitals, market badges, triage lookback |
| ✓ | **Infinite Edge plane** — macro radar (DXY/10Y), OBI schema, velocity RSI override, cold-start tick blend |
| ✓ | **Institutional Capital Harvesting** — Anti-Regret BE (+15 pips), 2R lock, parabolic £500 snap |
| ✓ | **Shadow trading engine** — `IG_AGENT_MODE=SHADOW` → `shadow_ledger.jsonl` |
| ✓ | **M-series thread affinity** — P-core / QoS pinning for execution + Lightstreamer |
| ✓ | **E2E stress framework** — S1 scenario replay, S2 capacity/lot gate (5k frames), S3 regional lifecycle |
| ✓ | **Broker lot contract** — 2-decimal truncation (`truncate_to_broker_lot`) pre-dispatch |
| ✓ | **Self-healing supervisor** — patch_crash_* branch gate + SYSTEM_HOT_RELOAD |

---

## In soak / monitoring

| Status | Capability |
|--------|------------|
| ◐ | Shadow vs live P&L attribution under 24/7 Mac Mini host |
| ◐ | Macro radar live DXY / 10Y feed (currently EUR/USD + Dow proxy) |
| ◐ | Full Level-2 order book ingress (OBI schema ready; hub-proxy L2 today) |

---

## Planned / future soak goals

| Status | Capability |
|--------|------------|
| ⏳ | Dynamic Spread-to-ATR News Spike Protection (Target: 20% limit) |
| ⏳ | Asymmetric Time-Based Stale Position Decay Exits |
| ⏳ | Correlation Density Confidence Floor Risk Scaler |

---

## Deferred pillar (post-soak)

- **Advanced AI Reward Optimization** — shift ML scorer from win-rate to profit-factor reward shaping; batch CSV import into `shadow_training_registry`.

---

## Reference documents

| Doc | Role |
|-----|------|
| `IG_Agent_v29.1_COMPLETE_SPEC.md` | Authoritative operator + implementer spec |
| `docs/V29.1_ARCHITECTURE.md` | Module map and data flow |
| `docs/MAINTENANCE_LOG.md` | Stress gates and re-deployment checklist |
| `docs/INTELLIGENCE_LAYER_BLUEPRINT.md` | Intelligence plane design notes |

---

## Phase 2 delivery log (2026-06-17)

1. **Co-Pilot Tactical Overhaul** — cockpit-web vitals banner, Card B badges, triage fallback, telemetry `global_ai_status_key` / `market_states_map`
2. **Infinite Edge Overhaul** — `macro_radar.py`, `OrderBookDepthPayload`, `shadow_executor.py`, `thread_affinity.py`, microstructure velocity RSI + warmup normalization
3. **Capital Harvesting Contract** — `apply_capital_harvest_contract()` in `intelligence/alpha_trail.py`; milestone snap in `target_engine.py`

**Regression:** `tests/test_deployed_fixes.py` (67) · `tests/stress/` (19) · cockpit + infinite edge suites green.
