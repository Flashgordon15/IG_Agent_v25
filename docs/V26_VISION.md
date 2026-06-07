# v26 — Vision: What It Looks Like When Built

A concrete picture of the **two-agent, feeder-fed** model on your MacBook.

---

## 1. One sentence

**v25** is the IG-connected **sensor and executor**; **v26** is the AI-driven **portfolio brain** that learns from v25's stream plus unlimited offline data — starting in shadow, earning order authority only after certification.

---

## 2. System picture

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR MACBOOK                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐         data_lake/          ┌──────────────┐  │
│  │  v25 AGENT   │ ─── events / fills ───────► │  v26 AGENT   │  │
│  │  (feeder)    │         features            │  (brain)     │  │
│  │              │ ◄── order intents (Phase3) │              │  │
│  │  IG REST/LS  │                             │  shadow│trade │  │
│  └──────┬───────┘                             └──────┬───────┘  │
│         │                                            │          │
│         ▼                                            ▼          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Dashboard :8080  —  LIVE | v26 SHADOW | CERT | PROFIT   │  │
│  └──────────────────────────────────────────────────────────┘  │
│         ▲                                                       │
│         │ nightly 22:30+                                        │
│  ┌──────┴───────┐                                               │
│  │ AI pipeline  │  train · walk-forward · certify · rollback    │
│  └──────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    IG DEMO → (later) IG LIVE
                    one order authority at a time
```

---

## 3. A trading day (Phase 2 — shadow)

| Time (BST) | v25 | v26 |
|------------|-----|-----|
| 00:00–07:00 | Trades Japan session; emits events | Scores 12 epics; logs **shadow intents** |
| 07:00 | Dashboard LIVE shows v25 positions | SHADOW tab shows "v26 would BUY Nasdaq" |
| 12:00 | Overlap — v25 may enter Wall St | v26 allocator picks best 3 uncorrelated intents |
| 16:00–22:00 | US session fills | Regime = RISK_ON; S2 momentum weight up |
| 22:30 | Session flatten (v25) | Ingest day → feature store → **nightly AI train** |
| 23:00 | Idle | Certification script updates L3/L4 counters |

**You open dashboard once:** compare v25 actual P&L vs v26 shadow P&L for the day.

---

## 4. What v26 can do that v25 cannot

| Capability | v25 | v26 |
|------------|-----|-----|
| Markets in live loop | Config-fixed (~4) | Universe 20+ in research; dynamic live set |
| Strategies | One rules engine | Registry: add/remove without touching execution |
| AI retrain | Light XGBoost | Full pipeline + versioning + rollback |
| Portfolio risk | Per-epic cap | £ heat, correlation, regime scale |
| Proof | Manual reports | L0–L5 automated certification |
| New epic | Config edit + restart | Universe manager promotes from research |
| AI-generated strategy | No | Plugin slot (human-gated deploy) |

v25 **does not block** these — v26 reads the lake and offline data, not v25's strategy code.

---

## 5. Learning — what "learn as much as possible" means

| Source | Volume | Used for |
|--------|--------|----------|
| v25 `signal_eval` (all WAIT/BUY/SELL) | Every 5m bar × markets | Gate attribution, shadow labels |
| v25 fills | Every close | Ground-truth P&L |
| v25 shadow log | Continues | Blocker analysis |
| Offline OHLC | Unlimited history | Walk-forward, new epics |
| Cross-asset series | DXY, VIX proxy, etc. | Regime features |
| Economic calendar | Events | Event regime |
| v26 shadow intents | Phase 1+ | Compare counterfactual before authority |

**AI learns offline.** Live loop loads **artifacts**, not training code — keeps MacBook responsive and REST budget safe.

---

## 6. Certification view (dashboard CERT tab)

```
┌─────────────────────────────────────────┐
│  v26 CERTIFICATION          Target: L5  │
├─────────────────────────────────────────┤
│  L0 P&L audit          ████████████ PASS│
│  L1 Replay 90d         ████████░░░░  67%│  need 30% days ≥ £1k
│  L2 Walk-forward 6m    ████████████ PASS│
│  L3 Shadow 14d         ██████░░░░░░  +£42 vs v25 E£
│  L4 Demo forward       ░░░░░░░░░░░░  not started
│  L5 10/14 ≥ £1k       ░░░░░░░░░░░░  not started
├─────────────────────────────────────────┤
│  Milestone: M2 (£500 median)  12d left  │
│  Order authority: v25 (feeder)            │
└─────────────────────────────────────────┘
```

---

## 7. Promotion moment (Phase 3)

When L5 passes:

1. Stop v25 order path (`feeder-only` or process stopped).  
2. Start `v26/main.py --mode trade`.  
3. Dashboard banner: **"v26 CERTIFIED — demo trading"**.  
4. Profit cap halts new entries at +£1k/day (proof preservation).  

v25 code **stays in repo** for execution primitives v26 calls — or v26 imports `src/execution/*` directly.

---

## 8. Live real money (Phase 4)

```
Demo L5 certified
       ↓
Human signs Live Promotion Checklist
       ↓
v26 --mode trade --account LIVE --size-factor 0.25
       ↓
14 days: live PF ≥ 1.2, slippage logged
       ↓
size-factor 0.50 → 1.00
```

Real funds use **same v26 brain** — not a third system.

---

## 9. File tree when mature

```
v26/
  main.py                    # --mode shadow|trade|research
  portfolio/allocator.py
  strategies/
    base.py
    s1_rules_v25.py          # parity wrapper
    s2_momentum.py
    s3_session_fx.py
    s4_ml_meta.py
  research/
    nightly_pipeline.py
    feature_store.py
    walk_forward.py
  certification/ladder.py
shared/contracts/event_schema.json
data_lake/
  events/2026-06-07.jsonl
  features/epic=NASDAQ/...
  models/v2026-06-07/
  certifications/2026-06-07.json
```

---

## 10. Is this a good approach?

**Yes**, if you treat:

- v25 = **feeder + execution library** (not the forever strategy)  
- v26 = **separate process, separate config, separate universe**  
- **One order authority** on IG  
- **Shadow before trade**  
- **Cert before live cash**  

**Bad** only if v26 is another monolith rewrite or runs orders alongside v25.

---

*Vision doc v1 — companion to V25_TO_V26_STRATEGY.md*
