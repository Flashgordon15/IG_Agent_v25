# Morning Report — 2026-06-07

Generated: 2026-06-07 22:44:38 local

## Overnight status

| Metric | Value |
|--------|-------|
| Agent health OK | False |
| Trading healthy | False |
| Quotes fresh | False (0/4) |
| Points / issues | ['quotes_stale:IX.D.NIKKEI.IFM.IP'] |
| Feeder events (UTC 2026-06-07) | 5354 |
| v26 shadow intents | 668 |
| Gate pass → trade attempts | 0 |
| Orders SUBMITTED (recent log) | 0 |
| Trades closed (log lines) | 0 |

## P&L (rolling fills from feeder)

- Trades: **0** | WR: **0.0%** | E£/trade: **+0.00** | Total: **£+0.00**

## v25 vs v26 shadow

```
=== Shadow compare — 2026-06-07 ===
v25 feeder:
  signal_eval would_fire: 0
  order_intents:         0
  fill_closes:           0
  fill_pnl_gbp:          +0.00
v26 shadow (S1_rules_v25):
  shadow_intents:        668
  would_trade:           0

Rolling 14d portfolio (fill_close):
  trades: 0  WR: 0.0%  E£: +0.00  total: +0.00
```

## v26 Strategy Brief — learn from every market

Your points + ladder + trailing model is the **v25 chassis**. v26 adds a **research brain** that learns from every feeder event and certifies strategies before they touch capital.

### What v25 already captures (feeder → data_lake)

| Factor | Status | Where |
|--------|--------|-------|
| **IG client sentiment** | Live | `environment_scorer` ±10 adj; dashboard + `signal_eval` |
| **Vol regime** | In setup_key | `vollow` / `volnormal` / `volhigh` in every signal |
| **Vol regime gate** | OFF (config) | `vol_regime_filter_enabled` — enable in shadow first |
| **Session** | Live | `asia_early`, `london_morning`, overlap, `us_afternoon` |
| **Points ladder** | Live | HEALTHY → size 1×–4×; CAUTION 0.5×; ladder 2→4 positions |
| **Trailing / BE** | Live | ATR trail 0.75×, BE 0.4×, partial 1.5R, limit extend |
| **ML blend** | ON | XGBoost veto candidate for v26 S4 — test in shadow |
| **Markets live** | 4 | gold, japan_225, nasdaq_100, wall_street |
| **Position ladder** | base 2, one_per_epic=False | `position_ladder.py` + points HEALTHY |
| **Safeguards** | Live | drawdown £500, correlation cap 5/dir, spread cap, cooldown |

### Gaps v26 must close (your priorities)

| Gap | v26 solution | Phase |
|-----|--------------|-------|
| **News / calendar** | `config/calendar.json` + Finnhub/IG econ API; block ±30m high-impact | P3–4 |
| **Volatility guards** | Regime router: widen stops in `volhigh`, block entries in `vollow` + news | P3 |
| **Multi-strategy** | S1 rules (live) + S2 momentum + S3 FX + S4 ML meta in **shadow** | P2 |
| **AI learns all markets** | Feature store per epic; walk-forward; ban negative-E£ setups | P1–2 |
| **Sentiment profit logic** | Fade `crowded_long`/`crowded_short` in S2/S4; log counterfactual in feeder | P2 |
| **regime_snapshot events** | Emit env fitness + vol + sentiment each bar → v26 training labels | P2 |
| **Flexibility** | Portfolio allocator shifts capital to winning strategy×market pairs | P3 |

### Multi-strategy registry (v26 brain)

```
S1_rules_v25   → indices + gold (baseline, matches v25 gates)
S2_momentum    → breakout + vol expansion (trend days, oil/indices)
S3_session_fx  → mean-reversion London/NY (EUR/USD, GBP/USD)
S4_ml_meta     → ensemble veto + rank (learns from ALL feeder fills)
```

Router: `regime = classify(vol, calendar, cross-asset)` → certified strategies compete → allocator picks highest **E£-adjusted** score. **Only one process sends IG orders** until L5 cert.

### AI learning plane (offline, unlimited data)

1. **Ingest** — every `signal_eval`, `gate_result`, `fill_close` from feeder
2. **Label** — WIN/LOSS, R-multiple, exit_reason (trail/partial/target)
3. **Attribute** — which factor (sentiment, vol, session) helped or hurt
4. **Walk-forward** — monthly OOS; no lookahead
5. **Promote** — only setups with n≥30 and E£>0 enter `setup_registry.json`
6. **Praise wins** — overweight live closes in training; replay tagged lower weight

### Safeguards vs flexibility (balance)

| Safeguard (never remove) | Flexibility (earn with data) |
|--------------------------|------------------------------|
| One order sender | Ladder 2→4 when HEALTHY + green book |
| £500 daily loss halt (v25) → £2k at £50k | Size tiers 1×–4× on cumulative points |
| Correlation cap 5/dir | More epics after replay WR ≥ 52% |
| News blackout ±30m | Strategy switch by regime, not threshold hack |
| L5 demo 10/14 days ≥ £1k | Profit cap halt new entries after £1k day |

### £1,000/day — concrete v26 path

**Math:** £1k ≈ 15–18 trades × £55–70 E£ at £50k (2% daily). Requires **breadth + edge**, not bigger bets.

| Week | Milestone | Target |
|------|-----------|--------|
| W1 | Feeder soak + shadow parity | S1 matches v25; feature store growing |
| W2 | S2 + S3 shadow; vol/news guards in shadow | 6–8 epics; ban negative setups |
| W3 | Portfolio allocator demo | M2 £500 median 14d daily |
| W4 | S4 ML veto + calendar live in shadow | M3 £750; PF ≥ 1.5 |
| W5–6 | L5 demo soak | **10/14 days ≥ £1,000** |
| W7+ | Live micro 25% | Slippage audit → scale |

**Tonight's test value:** Japan asia_early + daytime gold/US exercises points, trailing, sentiment, and feeder labels — v26 shadow records every intent for tomorrow's compare even when v25 does not fire.

Trailing config: trail=0.75×ATR, BE=0.4×ATR, partial@1.5R

## Quick actions (today)

| Priority | Action |
|----------|--------|
| P0 | `shadow_compare --process --expectancy` — ban negative-E£ setups |
| P0 | Restart agent once to pick up ladder config (`one_position_per_epic: false`) |
| P1 | Add `config/calendar.json` stub + shadow news guard (no live block yet) |
| P1 | Enable `vol_regime_filter` in v26 shadow only; measure blocked winners/losers |
| P2 | Implement S2_momentum shadow strategy on feeder `bar_close` |
| P2 | Emit `regime_snapshot` feeder events (sentiment + vol + points state) |

Expectancy snapshot: `/Users/chrisgordon/Desktop/IG_Agent_v25/data_lake/state/expectancy_snapshot.json`

---
*Auto-generated by scripts/morning_report_v26.py*