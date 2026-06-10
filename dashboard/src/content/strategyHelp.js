/**
 * Strategy help — mirrors live agent logic (config_v25.json + trading_loop.py).
 * Update when config or gate logic changes.
 */

export const STRATEGY_HELP_VERSION = "29.0.0";

export const HELP_SECTIONS = [
  {
    id: "overview",
    title: "How the agent trades",
    body:
      "Each enabled market runs its own loop every ~5 seconds. On each closed 5-minute bar the agent scores BUY and SELL, then runs seven gates in order. All gates must pass before an order is sent. Open trades are managed with broker stops, breakeven, trailing, and session-end flatten.",
    bullets: [
      "Signals use closed candles only — the open bar is ignored to avoid shifting RSI/EMA.",
      "One signal per closed bar per market (duplicates suppressed until the next bar).",
      "180 s cooldown between new entries on the same epic.",
      "Session-end flatten closes all positions before the market closes (if enabled).",
    ],
  },
  {
    id: "markets",
    title: "Active markets (config)",
    body: "Per-instrument settings override global defaults. Disabled instruments are not traded.",
    table: {
      headers: ["Market", "Size", "Stop (pts)", "Risk cap", "Min conf.", "Sessions"],
      rows: [
        ["Japan 225", "0.4", "45", "£100", "85%", "asia_early"],
        ["Wall Street", "0.3", "80", "£150", "70%", "overlap, us_afternoon"],
        ["Spot Gold", "6.0", "10", "£200", "80%", "london_morning, overlap, us_afternoon"],
        ["US Tech 100", "0.25", "100", "£150", "75%", "overlap, us_afternoon"],
      ],
    },
    bullets: [
      "Session windows (BST): asia_early 00–07, london_morning 07–12, overlap 12–16, us_afternoon 16–22, late 22–00.",
      "EUR/USD, GBP/USD, US Oil, Germany 40 are configured but currently disabled.",
    ],
  },
  {
    id: "signal",
    title: "Rule-based signal (0–100 score)",
    body:
      "Uses 5 m bars for entry and 15 m for trend. Fast EMA 9 / slow EMA 21, RSI 14, ATR 14. Raw score is the higher of BUY or SELL components; learning memory may add a bonus or penalty.",
    bullets: [
      "BUY: 15 m uptrend (+30), 5 m fast>slow EMA (+20), RSI ≥58 scaled up to +20, three rising closes (+10), tight spread (+0–20), two bullish candles (+10), EMA gap momentum (+0–10).",
      "SELL: mirror logic with RSI ≤45 and bearish structure.",
      "RSI hard block: BUY blocked if RSI >85; SELL blocked if RSI <15.",
      "Low ATR (below instrument min_atr_points) reduces score ×0.65. Wide spread reduces ×0.50.",
      "Learning: after 10+ trades on the same setup, win rate & avg P&L can add up to +8 or −15 to the score.",
      "Pre-filter floor: global signal_threshold 80 (instrument may be lower, e.g. 70 for indices).",
    ],
  },
  {
    id: "gates",
    title: "Entry gates (all must pass)",
    body: "Shown on the LIVE tab in this order. Any failure blocks a new entry for that tick.",
    bullets: [
      "1. session_open — IG market open, not in maintenance, inside instrument session whitelist, not blocked near session end.",
      "2. cold_start_gap — First 6 bars after open (cold start) blocked; gap-open >1× ATR blocked until 12 bars (~1 h) or 60 min wall-clock.",
      "3. environment_fitness — Composite score ≥55% (ATR, trend, session, spread factors). Cold-start/gap caps may apply.",
      "4. points_state — Not STOP, not session-pause (after 6 losses), daily loss <£500.",
      "5. risk_validation — Spread ≤2.5× normal (not 1.5× — code uses 2.5×), position slots free, £ risk within cap. Size clipped to risk cap if needed.",
      "6. signal_confidence — BUY or SELL with blended confidence ≥ effective threshold (see Points).",
      "7. execution — auto_trade on, adaptive checks pass, correlation guard OK, live arming ticks met, no pending order.",
    ],
  },
  {
    id: "points",
    title: "Points system",
    body:
      "Cumulative points drive agent state and how hard the bar is to trade. Scored on each closed trade from result, confidence band, and P&L (scaled after 5+ confirmed trades).",
    table: {
      headers: ["Cumulative", "State", "Entry bar", "Size effect"],
      rows: [
        ["> +4", "HEALTHY", "≥80% (floor rises with wins)", "Full tiered size (see below)"],
        ["−5 to +4", "CAUTION", "≥80%", "0.5× base when conf ≥80%"],
        ["−30 to −5", "WARNING", "≥92% only", "0.25× on high-conf only"],
        ["< −30", "STOP", "No entries", "0× — latched until manual reset"],
      ],
    },
    bullets: [
      "Confidence bands: high ≥92%, standard 85–91%, marginal 80–84%, low <80% (marginal wins score +1 pt; low scores 0).",
      "HEALTHY size tiers: cumulative >50 → up to 4×; >25 → 2.5×; >4 → 1.5×; else 1× — then ×0.5 (standard) or ×0.25 (marginal).",
      "Partial close: enabled at 1.5× ATR profit — banks 50% and scores points immediately.",
      "6 consecutive losses → skip next 1 actionable signal (session pause).",
      ">£2000 realised loss in 60 min → forced WARNING for 30 min.",
      "3 recovery wins improves effective state one notch; 5 wins → HEALTHY boost.",
      "Bootstrap: confidence_floor starts 80, +1 per win toward floor (capped at 80).",
    ],
  },
  {
    id: "sizing",
    title: "Position size & risk",
    body:
      "Planned size = base trade_size × points multiplier, clamped to 0.01–50 lots, raised to IG minimum, then clipped so stop × size × £/pt ≤ risk_cap_gbp.",
    bullets: [
      "Base sizes: Japan 225 0.4, Wall St 0.3, Gold 6.0, Nasdaq 0.25 (contracts/lots per IG).",
      "Adaptive engine may scale size: good setup (WR≥60%, avg>0, 6+ trades) ×3.0; bad setup ×0.2 or blocked entirely.",
      "High confidence ≥90%: reward target up to 3× stop (vs default 2×).",
      "Stop distance: ATR-based when enabled, clamped between adaptive_min/max risk points per instrument; dynamic floor uses session + vol regime.",
      "Max open: 15 total across all markets; 2 per epic (base). HEALTHY + all epic positions green + oldest ≥20 min → up to 4 per epic.",
      "Correlation guard: max 5 new entries per direction per calendar day (blocks one-way stacking).",
      "one_position_per_epic: true — no stacking on the same epic.",
      "Circuit breaker: 5 consecutive losses → 60 min pause; resume at half size.",
    ],
  },
  {
    id: "ml",
    title: "Machine learning blend",
    body:
      "Optional XGBoost model (USE_ML_SIGNAL=true). Trained on fired signals only; predicts win probability from rule score, RSI, and ATR ratio (ATR ÷ stop distance).",
    bullets: [
      "Blend only when ≥500 ML training records AND model probability is ≥15% away from 50%.",
      "Formula: 60% rules confidence + 40% × (ML prob × 100). Near-50% → rules score used unchanged.",
      "If model file missing or features incomplete, rules-only scoring applies.",
      "ML decisions appear in INTELLIGENCE tab (last 20 blends).",
      "Tuning: retrain via replay pipeline; disable with USE_ML_SIGNAL false in config.",
    ],
  },
  {
    id: "exits",
    title: "Trade management & exits",
    body: "Stops and targets set at entry from adaptive risk/limit. IG broker stops used when connected.",
    bullets: [
      "Initial stop: entry ± risk points (adaptive ATR-based risk). Target: entry ± limit (risk × reward multiple, capped by ATR).",
      "Breakeven: at +30 pts favourable move, stop moved to entry (+0 offset). Once per position.",
      "Trailing stop: enabled; triggers at +50 pts, trails 25 pts behind (adaptive bands may adjust).",
      "Max position age: 480 min (8 h) — stale positions closed.",
      "Session flatten: auto_flatten_on_session_end closes all before close when enabled.",
      "Manual: dashboard Close per position, Close All, Flatten, Emergency Stop.",
    ],
  },
  {
    id: "tuning",
    title: "Tuning guide (config_v25.json)",
    body: "Edit config then restart the agent. Instrument block overrides globals for that epic.",
    bullets: [
      "Stricter entries: raise signal_threshold / confidence_floor, or instrument signal_threshold.",
      "More size: raise trade_size or adaptive_max_trade_size (watch risk_cap_gbp).",
      "Tighter risk: lower risk_cap_gbp or stop_distance_points per instrument.",
      "Points sensitivity: HEALTHY threshold is cumulative >6 (code); WARNING at −30.",
      "Sessions: trading_session_whitelist per instrument — remove a session name to stop trading then.",
      "ML: USE_ML_SIGNAL, retrain model; blend weights are in trading_loop.py (60/40, 15% conviction).",
      "Spread: max_spread_pts per instrument; gate uses 2.5× rolling normal spread.",
      "Cooldown: cooldown_seconds (default 180). Daily halt: max_daily_loss_gbp (£500).",
    ],
  },
];
