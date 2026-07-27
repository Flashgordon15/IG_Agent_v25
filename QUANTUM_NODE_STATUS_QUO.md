# 📊 QUANTUM ENGINE SYSTEM ARCHITECTURE & STRATEGIC BLUEPRINT

## 1. THE STATUS QUO: DEBUNKING THE LAGGING METRICS
- **The Screen Metric Illusion:** The 'ML Confidence' gauge swinging between 10% and 85% is a UI telemetry proxy calculated using stack membership, z-score, and velocity. It is NOT the raw execution trigger.
- **The Balance Bleed Real-World Cause:** Our current intra-day drift (-£154.90) is caused by micro-trail stops getting harvested in noisy, low-edge CHOP regimes, compounded by a legacy backend accounting bug that records settlements as 19x CANCELLED and 77x BREAKEVEN with entry == exit.
- **The Current Telemetry Block:** The feed is currently fail-closed due to a quote age lag of ~224 seconds. The 0.0ms event lane is safely blocking entries because quote freshness exceeds our strict 500ms safety ceiling.

## 2. CORE EXECUTION & DECISION MATRIX (HOW TRADES ACTUALLY FIRE)
1. **Authoritative Dual-Core Rotation:** The engine prioritizes the Wall Street/DOW Index, completely isolating Nikkei executions until JPY accounting layers are certified.
2. **CPython In-Memory State Check:** The tick lane evaluates entries against an ultra-low-latency `RuntimeContext` memory bitmask in sub-nanoseconds, bypassing slow file system hot-reloads.
3. **Regime Veto Filter:** If the asset scanner reports a RANGE_BOUND or NEUTRAL condition, the strategy completely disarms entry loops, shifting to a passive scraping proxy phase.
4. **Order Dispatch Aggression:** Validated signals utilize aggressive native IG MARKET payloads injected with a strict `maxSlippage` constraint set at `max(1, round(0.5 * spread))` to eliminate queue hangovers.

## 3. CORE STRATEGIC RECONCILIATION PLAN (THE PATH TO >=60% WIN RATE)
### Phase A: RESTORING TELEMETRY TRUTH (IMMEDIATE)
- **Heal the Data Stream:** Force-restart the Lightstreamer streaming hub callbacks to bring the quote age from 224 seconds down to <10ms.
- **Commit the Flat-Session Memory Matrix:** Cycle the legacy process wrapper via `./scripts/desk_deploy.sh deploy` to load the newly pass-validated `WIN/LOSS/BREAKEVEN` gross GBP close-accounting blocks from disk into active memory.
- **Scrub Phantom Records:** Erase all hollow, unmonitored ghost rows from our display cache to prevent CPU thread congestion during volatile US sessions.

### Phase B: ALPHA OPTIMIZATION (TACTICAL EXTRACTION)
- **Tighten the Chop Filter:** Enforce a strict structural rule: zero entry deployment if the market scanner flags the index as RANGE_BOUND, regardless of operator sentiment.
- **Widen the Micro-Trail Breathing Room:** Slightly loosen the initial trailing stop or slow down the first trail floor activation parameter to prevent market maker micro-spread noise from harvesting small reds repeatedly.
- **Asymmetric Expectancy Scaling:** Restrict concurrent open positions to a maximum of 1 or 2, and dynamically widen the Take-Profit bracket to an aggressive 3.5x ATR ratio while locking the Stop-Loss tightly at 1.0x.
