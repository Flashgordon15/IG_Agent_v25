# IG Agent Core Platform: Strategic Enhancements Roadmap

Official architectural roadmap for quantitative upgrades **after** the v29.1 live soak phase establishes a stable performance baseline.

---

## Production complete (v29.1)

| Status | Capability |
|--------|------------|
| ✓ | Sub-microsecond Trailing Stop Evaluation Engine (~1.5µs execution) |
| ✓ | Asynchronous Non-Blocking Broker Stop Dispatch Worker |
| ✓ | Real-time 3-Stage Boot Progress Bar & Password Firewall |
| ✓ | IG 0.5% Commercial FX Fee & Automated Min-Distance Clamps |
| ✓ | 2-Per-Epic Allocation Cap Loops Priority Harmonization |

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

See architecture notes in `docs/V29.1_ARCHITECTURE.md` and learning plane boundaries in `IG_Agent_v29.1_COMPLETE_SPEC.md`.
