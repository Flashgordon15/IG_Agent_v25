#!/usr/bin/env python3
"""Generate overnight + v26 progress morning report."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.append(str(ROOT / "v26"))

OUT_DIR = ROOT / "docs" / "morning"


def _read_engine_tail(n: int = 5000) -> str:
    p = ROOT / "src" / "data" / "logs" / "engine.log"
    if not p.is_file():
        return ""
    try:
        return "\n".join(
            p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        )
    except OSError:
        return ""


def _overnight_stats(tail: str) -> dict:
    submitted = len(re.findall(r"action=SUBMITTED", tail))
    all_pass = len(re.findall(r"ALL GATES PASSED", tail))
    fill_close = tail.count("TRADE CLOSED")
    stale = len(re.findall(r"Quote stream stale", tail))
    market_closed = len(re.findall(r"market closed", tail))
    return {
        "gates_passed_attempts": all_pass,
        "orders_submitted": submitted,
        "trades_closed_log": fill_close,
        "stale_quote_blocks": stale,
        "market_closed_blocks": market_closed,
    }


def _load_v25_config() -> dict:
    p = ROOT / "config" / "config_v25.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _enabled_markets(cfg: dict) -> list[str]:
    instruments = cfg.get("instruments") or {}
    return sorted(
        k for k, v in instruments.items() if isinstance(v, dict) and v.get("enabled")
    )


def _v26_research_section() -> list[str]:
    """Overnight research: professional systems, proven strategies, AI incentives."""
    return [
        "## v26 Research Brief — for tomorrow's discussion",
        "",
        "Synthesis from institutional quant architecture, prop-firm risk practice, "
        "open-source multi-agent systems, and global session structure.",
        "",
        "### How professional systems make money (pattern, not magic)",
        "",
        "| Layer | What winners do | v26 mapping |",
        "|-------|-----------------|-------------|",
        "| **Data** | Unified state bus; Parquet/DuckDB; billions of bars offline | Feeder → feature store |",
        "| **Strategies** | 3–6 *independent* engines (momentum, MR, macro flow) | S1–S4 registry |",
        "| **Allocator** | QP / utility: max edge − risk − turnover penalty | Phase 3 portfolio heat |",
        "| **Regime** | Vol + correlation + trend → scale risk (γ adaptive) | `regime_filter.py` |",
        "| **Governance** | OK → DE_RISK → KILL above drawdown | Points STOP + £2k halt |",
        "| **Proof** | Walk-forward OOS before capital | L0–L5 certification |",
        "",
        "Key insight: **profits come from diversification across uncorrelated edges**, "
        "not one super-strategy. Renaissance/Two Sigma pattern = many small signals + "
        "strict risk budget (public descriptions, not proprietary alpha).",
        "",
        "### Proven strategy families to implement (lessen risk, raise E£)",
        "",
        "| Strategy | When it works | Risk control | v26 ID |",
        "|----------|---------------|--------------|--------|",
        "| **Trend / momentum** | London–NY overlap, `volhigh` | Wider trail; smaller size in chop | S2 |",
        "| **Mean reversion** | Asia range, FX London open | Tight stop; time stop 2h | S3 |",
        "| **Session breakout** | First 30m after cash open | News calendar block ±30m | S2 variant |",
        "| **Rules baseline** | All sessions (current v25) | Gate stack + points | S1 |",
        "| **ML meta veto** | All — blocks bad context | Hard block, never invent trades | S4 |",
        "| **Sentiment fade** | `crowded_long`/`short` extremes | Half size; counter-trend only | S4 feature |",
        "",
        "**Risk reducers pros always use:**",
        "- Fractional Kelly (25% of theoretical) — maps to your points size tiers",
        "- Risk capital = drawdown *cushion*, not nominal £50k",
        "- Turnover penalty — avoid overtrading after £1k day (profit cap)",
        "- Correlation clustering — your cap-5/dir guard + future £ heat",
        "",
        "### Global market clock (24h edge for CFD book)",
        "",
        "```",
        "00:00–07:00 BST  Asia      → Japan (S1 asia_early) — range/trend open",
        "07:00–12:00 BST  London    → Gold morning, EUR/GBP prep (S3 later)",
        "12:00–16:00 BST  Overlap   → PEAK liquidity — indices + gold (S1+S2)",
        "16:00–22:00 BST  US        → Wall St, Nasdaq, oil (S1+S2)",
        "22:00+           Flatten   → Research plane trains; no live REST burst",
        "```",
        "",
        "London–NY overlap (12:00–16:00 BST) = **highest E£/hour** — allocator should "
        "shift budget here when regime = RISK_ON.",
        "",
        "### How to incentivise AI to succeed (safely)",
        "",
        "Your **points engine is already a human-aligned reward function**. v26 extends it:",
        "",
        "| Mechanism | Incentive | Anti-gaming |",
        "|-----------|-----------|-------------|",
        "| Points bands | Reward high-conf wins more | Marginal wins flat; losses scaled |",
        "| HEALTHY ladder | More size/positions when green | CAUTION blocks ladder |",
        "| Setup registry BAN | Negative E£ → zero capital | Needs n≥30 samples |",
        "| Live > replay weight | Promote what works live | Replay tagged lower |",
        "| Certification L1–L5 | AI only earns order authority | Shadow until pass |",
        "| ML training target | R-multiple + capture_ratio | Not raw P&L alone |",
        "",
        "**Offline AI reward (research plane only):**",
        "```",
        "R = w1·E£ − w2·drawdown² − w3·volatility − w4·turnover − w5·spread_cost",
        "```",
        "Weights tuned on walk-forward — never optimise live loop directly.",
        "",
        "### Global system factors (cross-market edge)",
        "",
        "| Factor | Source | v26 use |",
        "|--------|--------|---------|",
        "| DXY / USD strength | Yahoo bulk | Risk-off scale-down |",
        "| VIX proxy | Index vol | Regime DE_RISK trigger |",
        "| IG client sentiment | REST (per session) | Fade crowded; feeder label |",
        "| Economic calendar | Finnhub / ForexFactory API | Entry block ±30m |",
        "| Cross-asset correlation | Feeder positions | Reduce size when 3+ same dir |",
        "| Vol percentile 20d | OHLC cache | `vollow` block / `volhigh` trail widen |",
        "",
        "### Tomorrow discussion agenda (v26 approach for all)",
        "",
        "1. **Confirm architecture** — v25 feeder + v26 brain + one order sender",
        "2. **Strategy priority** — S2 momentum before FX? (overlap has most data)",
        "3. **News API** — free tier Finnhub vs manual `calendar.json` first?",
        "4. **Allocator math** — simple utility scores → full QP in Phase 4?",
        "5. **£1k proof** — M4 = 10/14 days; profit cap halts new entries",
        "6. **AI scope** — offline train only; live loads artifacts (thresholds, weights)",
        "7. **Restart agent** — pick up ladder config if not done overnight",
        "",
        "### Recommended v26 success formula",
        "",
        "```",
        "Edge (multi-strategy OOS) × Frequency (12–18 trades, 8+ epics)",
        "× Capture (trail + partial, capture_ratio ≥ 0.55)",
        "÷ Friction (spread + slippage ≤ 15% gross)",
        "= £1,000/day at £50k (certified, not hoped)",
        "```",
        "",
    ]


def _v26_strategy_section(cfg: dict) -> list[str]:
    """v26 north-star: learn from every market, multi-strategy, safeguards + flexibility."""
    enabled = _enabled_markets(cfg)
    vol_filter = cfg.get("vol_regime_filter_enabled", False)
    ml_on = cfg.get("USE_ML_SIGNAL", False)
    max_epic = cfg.get("max_positions_per_epic", 2)
    one_per = cfg.get("one_position_per_epic", True)
    trailing = (
        (cfg.get("trailing_stop") or {})
        if isinstance(cfg.get("trailing_stop"), dict)
        else {}
    )

    return [
        "## v26 Strategy Brief — learn from every market",
        "",
        "Your points + ladder + trailing model is the **v25 chassis**. v26 adds a "
        "**research brain** that learns from every feeder event and certifies strategies "
        "before they touch capital.",
        "",
        "### What v25 already captures (feeder → data_lake)",
        "",
        "| Factor | Status | Where |",
        "|--------|--------|-------|",
        "| **IG client sentiment** | Live | `environment_scorer` ±10 adj; dashboard + `signal_eval` |",
        "| **Vol regime** | In setup_key | `vollow` / `volnormal` / `volhigh` in every signal |",
        f"| **Vol regime gate** | {'ON' if vol_filter else 'OFF (config)'} | `vol_regime_filter_enabled` — enable in shadow first |",
        "| **Session** | Live | `asia_early`, `london_morning`, overlap, `us_afternoon` |",
        "| **Points ladder** | Live | HEALTHY → size 1×–4×; CAUTION 0.5×; ladder 2→4 positions |",
        "| **Trailing / BE** | Live | ATR trail 0.75×, BE 0.4×, partial 1.5R, limit extend |",
        f"| **ML blend** | {'ON' if ml_on else 'OFF'} | XGBoost veto candidate for v26 S4 — test in shadow |",
        f"| **Markets live** | {len(enabled)} | {', '.join(enabled) or 'none'} |",
        f"| **Position ladder** | base {max_epic}, one_per_epic={one_per} | `position_ladder.py` + points HEALTHY |",
        "| **Safeguards** | Live | drawdown £500, correlation cap 5/dir, spread cap, cooldown |",
        "",
        "### Gaps v26 must close (your priorities)",
        "",
        "| Gap | v26 solution | Phase |",
        "|-----|--------------|-------|",
        "| **News / calendar** | `config/calendar.json` + Finnhub/IG econ API; block ±30m high-impact | P3–4 |",
        "| **Volatility guards** | Regime router: widen stops in `volhigh`, block entries in `vollow` + news | P3 |",
        "| **Multi-strategy** | S1 rules (live) + S2 momentum + S3 FX + S4 ML meta in **shadow** | P2 |",
        "| **AI learns all markets** | Feature store per epic; walk-forward; ban negative-E£ setups | P1–2 |",
        "| **Sentiment profit logic** | Fade `crowded_long`/`crowded_short` in S2/S4; log counterfactual in feeder | P2 |",
        "| **regime_snapshot events** | Emit env fitness + vol + sentiment each bar → v26 training labels | P2 |",
        "| **Flexibility** | Portfolio allocator shifts capital to winning strategy×market pairs | P3 |",
        "",
        "### Multi-strategy registry (v26 brain)",
        "",
        "```",
        "S1_rules_v25   → indices + gold (baseline, matches v25 gates)",
        "S2_momentum    → breakout + vol expansion (trend days, oil/indices)",
        "S3_session_fx  → mean-reversion London/NY (EUR/USD, GBP/USD)",
        "S4_ml_meta     → ensemble veto + rank (learns from ALL feeder fills)",
        "```",
        "",
        "Router: `regime = classify(vol, calendar, cross-asset)` → certified strategies "
        "compete → allocator picks highest **E£-adjusted** score. **Only one process "
        "sends IG orders** until L5 cert.",
        "",
        "### AI learning plane (offline, unlimited data)",
        "",
        "1. **Ingest** — every `signal_eval`, `gate_result`, `fill_close` from feeder",
        "2. **Label** — WIN/LOSS, R-multiple, exit_reason (trail/partial/target)",
        "3. **Attribute** — which factor (sentiment, vol, session) helped or hurt",
        "4. **Walk-forward** — monthly OOS; no lookahead",
        "5. **Promote** — only setups with n≥30 and E£>0 enter `setup_registry.json`",
        "6. **Praise wins** — overweight live closes in training; replay tagged lower weight",
        "",
        "### Safeguards vs flexibility (balance)",
        "",
        "| Safeguard (never remove) | Flexibility (earn with data) |",
        "|--------------------------|------------------------------|",
        "| One order sender | Ladder 2→4 when HEALTHY + green book |",
        "| £500 daily loss halt (v25) → £2k at £50k | Size tiers 1×–4× on cumulative points |",
        "| Correlation cap 5/dir | More epics after replay WR ≥ 52% |",
        "| News blackout ±30m | Strategy switch by regime, not threshold hack |",
        "| L5 demo 10/14 days ≥ £1k | Profit cap halt new entries after £1k day |",
        "",
        "### £1,000/day — concrete v26 path",
        "",
        "**Math:** £1k ≈ 15–18 trades × £55–70 E£ at £50k (2% daily). Requires **breadth + edge**, not bigger bets.",
        "",
        "| Week | Milestone | Target |",
        "|------|-----------|--------|",
        "| W1 | Feeder soak + shadow parity | S1 matches v25; feature store growing |",
        "| W2 | S2 + S3 shadow; vol/news guards in shadow | 6–8 epics; ban negative setups |",
        "| W3 | Portfolio allocator demo | M2 £500 median 14d daily |",
        "| W4 | S4 ML veto + calendar live in shadow | M3 £750; PF ≥ 1.5 |",
        "| W5–6 | L5 demo soak | **10/14 days ≥ £1,000** |",
        "| W7+ | Live micro 25% | Slippage audit → scale |",
        "",
        "**Tonight's test value:** Japan asia_early + daytime gold/US exercises points, "
        "trailing, sentiment, and feeder labels — v26 shadow records every intent for "
        "tomorrow's compare even when v25 does not fire.",
        "",
        f"Trailing config: trail={trailing.get('trail_trigger_atr_multiple', 'n/a')}×ATR, "
        f"BE={trailing.get('breakeven_trigger_atr_multiple', 'n/a')}×ATR, "
        f"partial@{trailing.get('partial_close_at_r', 'n/a')}R",
        "",
    ]


def _health() -> dict:
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://localhost:8080/api/health", timeout=10
        ) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> int:
    from expectancy.engine import (
        collect_fills,
        compute_setup_stats,
        portfolio_summary,
        write_snapshot,
    )
    from ingest.lake_reader import summarize_day

    day_local = datetime.now().strftime("%Y-%m-%d")
    day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"MORNING_REPORT_{day_local}.md"

    cfg = _load_v25_config()
    health = _health()
    v25_sum = summarize_day(day_utc)
    shadow_path = ROOT / "data_lake" / "shadow_v26" / f"{day_utc}.jsonl"
    shadow_lines = 0
    if shadow_path.is_file():
        shadow_lines = sum(1 for _ in shadow_path.open() if _.strip())

    tail = _read_engine_tail()
    overnight = _overnight_stats(tail)

    fills = collect_fills(days=3)
    pf = portfolio_summary(fills)
    setups = compute_setup_stats(fills)[:8]
    try:
        snap_path = write_snapshot(days=14)
    except Exception:
        snap_path = None

    # shadow compare inline
    sc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "shadow_compare.py"), "--day", day_utc],
        cwd=str(ROOT),
        env={
            **dict(__import__("os").environ),
            "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'v26'}",
        },
        capture_output=True,
        text=True,
    )

    lines = [
        f"# Morning Report — {day_local}",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local",
        "",
        "## Overnight status",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Agent health OK | {health.get('ok', False)} |",
        f"| Trading healthy | {health.get('trading_healthy', 'n/a')} |",
        f"| Quotes fresh | {health.get('quotes_fresh', 'n/a')} ({health.get('quotes_fresh_count', 0)}/{health.get('quotes_total', 4)}) |",
        f"| Points / issues | {health.get('issues', [])} |",
        f"| Feeder events (UTC {day_utc}) | {v25_sum.total_events} |",
        f"| v26 shadow intents | {shadow_lines} |",
        f"| Gate pass → trade attempts | {overnight['gates_passed_attempts']} |",
        f"| Orders SUBMITTED (recent log) | {overnight['orders_submitted']} |",
        f"| Trades closed (log lines) | {overnight['trades_closed_log']} |",
        "",
        "## P&L (rolling fills from feeder)",
        "",
        f"- Trades: **{pf['n']}** | WR: **{pf['wr']:.1%}** | E£/trade: **{pf['e_gbp']:+.2f}** | Total: **£{pf['total_pnl_gbp']:+.2f}**",
        "",
    ]
    if setups:
        lines.append("### Top setups")
        lines.append("")
        for s in setups:
            lines.append(
                f"- `{s.setup_key[:50]}` — n={s.n} E£={s.e_gbp:+.2f} WR={s.wr:.0%} [{s.status}]"
            )
        lines.append("")

    lines.extend(
        [
            "## v25 vs v26 shadow",
            "",
            "```",
            (sc.stdout or sc.stderr or "(no output)").strip(),
            "```",
            "",
        ]
    )
    lines.extend(_v26_strategy_section(cfg))
    lines.extend(_v26_research_section())
    lines.extend(
        [
            "## Quick actions (today)",
            "",
            "| Priority | Action |",
            "|----------|--------|",
            "| P0 | `shadow_compare --process --expectancy` — ban negative-E£ setups |",
            "| P0 | Restart agent once to pick up ladder config (`one_position_per_epic: false`) |",
            "| P1 | Add `config/calendar.json` stub + shadow news guard (no live block yet) |",
            "| P1 | Enable `vol_regime_filter` in v26 shadow only; measure blocked winners/losers |",
            "| P2 | Implement S2_momentum shadow strategy on feeder `bar_close` |",
            "| P2 | Emit `regime_snapshot` feeder events (sentiment + vol + points state) |",
            "",
            f"Expectancy snapshot: `{snap_path}`" if snap_path else "",
            "",
            "---",
            "*Auto-generated by scripts/morning_report_v26.py*",
        ]
    )

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
