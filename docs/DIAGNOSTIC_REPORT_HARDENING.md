# Diagnostic Report — Rate Budget, Gold P&L, Data Separation, Multi-Market

**Date:** 2026-06-30  
**Scope:** IG Agent v31 demo throughput hardening pass

## 1. Gold P&L clustering (±£20)

### Root cause
Micro scalps used **fixed 1.5pt TP / 2.0pt SL** (`MICRO_TP_POINTS` / `INTERNAL_RISK_CEILING_PTS`) combined with **large deal sizes** (Gold CFD min ≈ 10 contracts) and **$1/point/contract** (`open_position_view` spec).

```
P&L_gbp ≈ points × size × point_value_gbp
        ≈ 2.0 × 10 × ~£0.79 ≈ £15–20
```

This produced tightly clustered wins/losses regardless of volatility.

### Fix
- **`execution/micro_risk_profile.py`** — TP/SL derived from `micro_risk.risk_per_trade_gbp`, `target_r_multiple`, `max_loss_cap_pts`
- **`config_v31_demo_throughput.json`** — `micro_risk` block (default £5 risk, 1.5R target, 4pt cap)
- **`virtual_stop_loss.py`** — per-trade `ceiling_pts` from risk profile (not hard-coded 2.0)
- **Spread-bet product** — smaller min deal vs CFD size=10

### Verify
New trades should show P&L spread proportional to `risk_per_trade_gbp / (size × point_value)` once IG rate limit clears.

## 2. IG rate-budget monitor & stubborn guard

### Implementation
- **`system/ig_budget_monitor.py`** — 30m rolling call counts, endpoint breakdown, budget estimate
- **`GET /api/ig_budget_state`** — `rate_limited`, `cooldown_until`, `execution_paused`, `calls_last_30m`
- **`execution/ig_execution_guard.py`** — blocks **order submission only** when rate-limited; signals continue
- **Cockpit `IgBudgetBanner`** — amber banner when execution paused

### Behaviour
When `rate_limited=true`: pierce/signal logic runs; `_dispatch_micro_order` returns early with `ig_rate_limited:Ns`. Auto-resumes when `RateLimitManager` cooldown expires.

## 3. Data vs execution separation

### Policy
See **`docs/IG_EXECUTION_YAHOO_DATA.md`**

- **Yahoo** — quotes, Z-scores, rotation velocity
- **IG REST** — orders, confirms, position sync only

### Enforcement
- **`system/data_execution_policy.py`** — warns on IG market REST while Yahoo poller active
- Hook in **`ig_api/rest_client.py`** `request()` after budget acquire

## 4. Multi-market trading

### Issues found
- `ACTIVE_STACK_SLOTS=2` limited hot stack to DOW+Gold only
- Rate limit blocked all markets simultaneously
- Rotation state lacked eligibility breakdown

### Fix
- **`dual_core.active_stack_slots: 3`** in demo config (DOW + Gold + Nikkei)
- **`get_rotation_eligibility()`** — `active_instruments`, `eligible_instruments`, `inactive_instruments` with reasons
- Enhanced **`/api/rotation_state`**

## 5. Validation

```bash
PYTHONPATH=src python3 scripts/demo_trading_validation.py --api-only
PYTHONPATH=src python3 -m pytest tests/test_ig_budget_and_risk.py -q
```

When IG cooldown clears:
```bash
PYTHONPATH=src python3 scripts/demo_trading_validation.py --wait-trades-sec 180
```

## Files changed (summary)

| Area | Files |
|------|-------|
| IG budget | `ig_budget_monitor.py`, `ig_execution_guard.py`, `rest_api_budget.py`, routes, `IgBudgetBanner.tsx` |
| P&L risk | `micro_risk_profile.py`, `virtual_stop_loss.py`, `dual_core_execution.py`, `trade_manager.py`, config |
| Data policy | `data_execution_policy.py`, `rest_client.py`, `IG_EXECUTION_YAHOO_DATA.md` |
| Multi-market | `dual_core_execution.py` (slots + eligibility), config |
| Validation | `demo_trading_validation.py`, `test_ig_budget_and_risk.py` |
