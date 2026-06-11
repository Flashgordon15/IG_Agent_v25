# Live Promotion Checklist (real funds)

Complete **every item** before v26 trades LIVE with real cash.  
Demo certification (L5) is necessary but **not sufficient**.

---

## A. Certification (automated)

- [ ] **L0** P&L audit: learning DB matches IG transaction sync (14d)  
- [ ] **L1** Replay 90d: ≥30% days ≥ £1k at £50k envelope (or your live-scaled target)  
- [ ] **L2** Walk-forward 6m: OOS profit factor ≥ 1.4  
- [ ] **L3** Shadow 14d: v26 shadow E£ ≥ v25 actual E£  
- [ ] **L5** Demo forward: 10/14 days ≥ £1k; friction ≤ 15% of gross wins  

## B. Operational

- [ ] v25 feeder events: 30+ days with &lt;1% gap rate  
- [ ] Watchdog + graceful shutdown tested  
- [ ] `HALT` flag tested (both agents respect)  
- [ ] Model rollback tested (revert to `models/v{previous}/`)  
- [ ] Telegram alerts on (optional but recommended for live)  

## C. Human capital decision (fill in)

| Field | Your value |
|-------|------------|
| Live account size £ | _________ |
| Max daily loss £ (hard halt) | _________ (suggest 2–4% of account) |
| Starting size factor | _________ (suggest **0.25**) |
| Core epics for first 14d | _________ (suggest 4 only) |
| Date signed | _________ |

## D. Live probation gates

| Week | Size factor | Pass to advance |
|------|-------------|-----------------|
| 1–2 | 0.25 | PF ≥ 1.0, max DD within halt |
| 3–4 | 0.50 | PF ≥ 1.2, live E£ ≥ 60% demo E£ |
| 5+ | 1.00 | PF ≥ 1.4, 14d median meets milestone |

## E. Abort rules (mandatory)

- Live 5-day rolling E£ &lt; 0 → **stop live**, return demo shadow  
- Live slippage &gt; 2× demo assumption → halve size  
- Any unexplained P&L mismatch → halt until L0 re-pass  

---

**Signature:** _________________________  **Date:** ___________
