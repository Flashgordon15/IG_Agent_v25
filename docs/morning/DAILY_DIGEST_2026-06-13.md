# Daily Operator Digest — 2026-06-13

*Generated Saturday 13 Jun 2026, 07:30 BST*

## At a glance

| | |
|---|---|
| Roadmap progress | **37%** (-11 vs yesterday) · milestone **M0** |
| Agent running | **No — start before sessions** |
| Demo soak | **ON** |
| 14d P&L | **£-4430.03** · WR **25.0%** · 36 closes |
| Today (feeder) | trade_ready **0** · intents **0** · closes **0** |
| ML training rows | **14** (target 500+) |
| Top gate blocker (7d) | **session_open** (49%) |

## Overnight (recent engine.log tail)

- Gates passed: **0**
- Orders submitted: **0**
- Trades closed (log): **0**
- Stale quote blocks: **0**

## Certification

L0 [PASS] 100% — P&L audit: Rolling expectancy snapshot from feeder fills · L1 [INSUFFICIENT] 14% — Soak 14d: 2/14 soak days · median £582.25 · L2 [PASS] 100% — Walk-forward: 6/6 epics with threshold curve

## Today's session map (BST)

| Window | Session | Markets |
|--------|---------|---------|
| 00:00–06:59 | `asia_early` | Japan 225 |
| 07:00–11:59 | `london_morning` | Gold, EUR/USD, GBP/USD |
| 12:00–15:59 | `london_us_overlap` | All except Japan (peak liquidity) |
| 16:00–21:59 | `us_afternoon` | US indices, oil, FX, gold |
| 22:00+ | `late` | Flat — no new entries |

## Trade outlook

- **Baseline:** ~2.6 closes/day from 14d ledger (recent active days were 8–10).
- **Today (agent down):** expect **0 trades** until main.py is running.

## Roadmap progress (£1k/day cert)

- **Overall:** 37% (-11 vs yesterday) · milestone **M0**

| Section | Today | Δ vs yesterday |
|---------|-------|----------------|
| Certification | 39% | -6 |
| Edge & ML | 17% | ±0 |
| Coverage | 75% | +1 |
| Trading flow | 17% | -39 |

- 14d net: **£-4430.03** · WR **25.0%** · trades **36**
- Today: trade_ready **0** · intents **0** · closes **0**

## If you only have 2 minutes

1. **Agent up?** Dashboard → Live tab, or `curl -s localhost:8080/api/health`.
2. **First session window:** Japan from ~00:00 BST; London FX/Gold from 07:00; overlap 12:00.
3. **Check intents > 0** during a session — if trade_ready > 0 but intents stay 0, restart once.
4. Read full archive: `docs/morning/DAILY_DIGEST_LATEST.md`

---
*Scheduled job: `com.igagent.v29digest` · `scripts/daily_operator_digest.py`*
