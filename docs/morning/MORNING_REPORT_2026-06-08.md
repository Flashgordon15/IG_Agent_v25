# Morning Report — 2026-06-08

Generated: 2026-06-08 08:02:13 local

## Overnight status

| Metric | Value |
|--------|-------|
| Agent health OK | True |
| Trading healthy | True |
| Quotes fresh | True (4/4) |
| Points / issues | [] |
| Feeder events (UTC 2026-06-08) | 131117 |
| v26 shadow intents | 0 |
| Gate pass → trade attempts | 0 |
| Orders SUBMITTED (recent log) | 0 |
| Trades closed (log lines) | 0 |

## P&L (rolling fills from feeder)

- Trades: **5** | WR: **20.0%** | E£/trade: **-7.34** | Total: **£-36.70**

### Top setups

- `SELL|bear|asia_early|atr120-150|rsilow|volnormal` — n=1 E£=+38.60 WR=100% [INSUFFICIENT]
- `SELL|bear|asia_early|atr120-150|rsimid|volnormal` — n=1 E£=-22.70 WR=0% [INSUFFICIENT]
- `SELL|bear|asia_early|atr180-210|rsilow|volnormal` — n=3 E£=-17.53 WR=0% [INSUFFICIENT]

## v25 vs v26 shadow

```
=== Shadow compare — 2026-06-08 ===
v25 feeder:
  signal_eval would_fire: 25
  order_intents:         11
  fill_closes:           5
  fill_pnl_gbp:          -36.70
v26 shadow (S1_rules_v25):
  shadow_intents:        0
  would_trade:           0
  parity vs would_fire:  0.0%
  vs order_intents:      0.0% (capped)

Rolling 14d portfolio (fill_close):
  trades: 5  WR: 20.0%  E£: -7.34  total: -36.70

Top setups by P&L:
  SELL|bear|asia_early|atr120-150|rsilow|volnormal n=  1 E£=+38.60 WR=100% [INSUFFICIENT]
  SELL|bear|asia_early|atr120-150|rsimid|volnormal n=  1 E£=-22.70 WR=0% [INSUFFICIENT]
  SELL|bear|asia_early|atr180-210|rsilow|volnormal n=  3 E£=-17.53 WR=0% [INSUFFICIENT]
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

## v26 Research Brief — for tomorrow's discussion

Synthesis from institutional quant architecture, prop-firm risk practice, open-source multi-agent systems, and global session structure.

### How professional systems make money (pattern, not magic)

| Layer | What winners do | v26 mapping |
|-------|-----------------|-------------|
| **Data** | Unified state bus; Parquet/DuckDB; billions of bars offline | Feeder → feature store |
| **Strategies** | 3–6 *independent* engines (momentum, MR, macro flow) | S1–S4 registry |
| **Allocator** | QP / utility: max edge − risk − turnover penalty | Phase 3 portfolio heat |
| **Regime** | Vol + correlation + trend → scale risk (γ adaptive) | `regime_filter.py` |
| **Governance** | OK → DE_RISK → KILL above drawdown | Points STOP + £2k halt |
| **Proof** | Walk-forward OOS before capital | L0–L5 certification |

Key insight: **profits come from diversification across uncorrelated edges**, not one super-strategy. Renaissance/Two Sigma pattern = many small signals + strict risk budget (public descriptions, not proprietary alpha).

### Proven strategy families to implement (lessen risk, raise E£)

| Strategy | When it works | Risk control | v26 ID |
|----------|---------------|--------------|--------|
| **Trend / momentum** | London–NY overlap, `volhigh` | Wider trail; smaller size in chop | S2 |
| **Mean reversion** | Asia range, FX London open | Tight stop; time stop 2h | S3 |
| **Session breakout** | First 30m after cash open | News calendar block ±30m | S2 variant |
| **Rules baseline** | All sessions (current v25) | Gate stack + points | S1 |
| **ML meta veto** | All — blocks bad context | Hard block, never invent trades | S4 |
| **Sentiment fade** | `crowded_long`/`short` extremes | Half size; counter-trend only | S4 feature |

**Risk reducers pros always use:**
- Fractional Kelly (25% of theoretical) — maps to your points size tiers
- Risk capital = drawdown *cushion*, not nominal £50k
- Turnover penalty — avoid overtrading after £1k day (profit cap)
- Correlation clustering — your cap-5/dir guard + future £ heat

### Global market clock (24h edge for CFD book)

```
00:00–07:00 BST  Asia      → Japan (S1 asia_early) — range/trend open
07:00–12:00 BST  London    → Gold morning, EUR/GBP prep (S3 later)
12:00–16:00 BST  Overlap   → PEAK liquidity — indices + gold (S1+S2)
16:00–22:00 BST  US        → Wall St, Nasdaq, oil (S1+S2)
22:00+           Flatten   → Research plane trains; no live REST burst
```

London–NY overlap (12:00–16:00 BST) = **highest E£/hour** — allocator should shift budget here when regime = RISK_ON.

### How to incentivise AI to succeed (safely)

Your **points engine is already a human-aligned reward function**. v26 extends it:

| Mechanism | Incentive | Anti-gaming |
|-----------|-----------|-------------|
| Points bands | Reward high-conf wins more | Marginal wins flat; losses scaled |
| HEALTHY ladder | More size/positions when green | CAUTION blocks ladder |
| Setup registry BAN | Negative E£ → zero capital | Needs n≥30 samples |
| Live > replay weight | Promote what works live | Replay tagged lower |
| Certification L1–L5 | AI only earns order authority | Shadow until pass |
| ML training target | R-multiple + capture_ratio | Not raw P&L alone |

**Offline AI reward (research plane only):**
```
R = w1·E£ − w2·drawdown² − w3·volatility − w4·turnover − w5·spread_cost
```
Weights tuned on walk-forward — never optimise live loop directly.

### Global system factors (cross-market edge)

| Factor | Source | v26 use |
|--------|--------|---------|
| DXY / USD strength | Yahoo bulk | Risk-off scale-down |
| VIX proxy | Index vol | Regime DE_RISK trigger |
| IG client sentiment | REST (per session) | Fade crowded; feeder label |
| Economic calendar | Finnhub / ForexFactory API | Entry block ±30m |
| Cross-asset correlation | Feeder positions | Reduce size when 3+ same dir |
| Vol percentile 20d | OHLC cache | `vollow` block / `volhigh` trail widen |

### Tomorrow discussion agenda (v26 approach for all)

1. **Confirm architecture** — v25 feeder + v26 brain + one order sender
2. **Strategy priority** — S2 momentum before FX? (overlap has most data)
3. **News API** — free tier Finnhub vs manual `calendar.json` first?
4. **Allocator math** — simple utility scores → full QP in Phase 4?
5. **£1k proof** — M4 = 10/14 days; profit cap halts new entries
6. **AI scope** — offline train only; live loads artifacts (thresholds, weights)
7. **Restart agent** — pick up ladder config if not done overnight

### Recommended v26 success formula

```
Edge (multi-strategy OOS) × Frequency (12–18 trades, 8+ epics)
× Capture (trail + partial, capture_ratio ≥ 0.55)
÷ Friction (spread + slippage ≤ 15% gross)
= £1,000/day at £50k (certified, not hoped)
```

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