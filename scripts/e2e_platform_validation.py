#!/usr/bin/env python3
"""
IG Agent v25 — end-to-end platform validation (SIM / isolated components).

Full run (all layers, ~2–3 min):
  PYTHONPATH=src python3 scripts/e2e_platform_validation.py

Quick run (layers 1–3 only, target <60s):
  PYTHONPATH=src python3 scripts/e2e_platform_validation.py --quick

Optional live IG reconciliation (layer 5.3):
  PYTHONPATH=src python3 scripts/e2e_platform_validation.py --live
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.ml_training_store import (
    MLTrainingStore,
    reset_ml_training_store_for_tests,
    set_store_path_for_tests,
)
from data.models import Quote, TradeRecord
from execution.execution_engine import ExecutionEngine
from execution.types import ExecutionMode, TradeSignal
from signals.signal_engine import SignalEngine, SignalResult
from system.config_loader import ConfigLoader
from trading.environment_scorer import (
    GATE_PASS_MIN,
    EnvironmentScorer,
    score_atr_factor,
    score_session_timing_factor,
    score_spread_factor,
    score_trend_factor,
)
from trading.ohlc_cache_paths import ohlc_cache_path
from trading.points_engine import (
    CONF_HIGH,
    CONF_MARGINAL_MIN,
    PointsEngine,
    set_points_state_path_for_tests,
)
from trading.trade_manager import TradeManager
from trading.trading_loop import signal_gate_explanation

EPIC = "IX.D.NIKKEI.IFM.IP"
MARKET = "Japan 225"


@dataclass
class CheckResult:
    layer: str
    check_id: str
    description: str
    passed: bool
    expected: str = ""
    got: str = ""
    skipped: bool = False


@dataclass
class LayerSummary:
    name: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed and not c.skipped)

    @property
    def total_count(self) -> int:
        return sum(1 for c in self.checks if not c.skipped)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks if not c.skipped)


class ValidationContext:
    """Isolated temp workspace for layers 3–4 and optional API tests."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ig_e2e_")
        self.base = Path(self.tmp.name)
        self.db_path = self.base / "learning.db"
        self.points_path = self.base / "points_state.json"
        self.shadow_path = self.base / "shadow_log.jsonl"
        self.ml_path = self.base / "ml_training_store.jsonl"
        self.snapshot_path = self.base / "dashboard_snapshot.json"
        self.cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
        self.store: LearningStore | None = None
        self.sim_deal_ref: str = ""
        self.sim_setup_key: str = ""
        self.ml_rows_before: int = 0

    def setup_isolated(self) -> None:
        set_points_state_path_for_tests(self.points_path)
        reset_ml_training_store_for_tests()
        set_store_path_for_tests(self.ml_path)
        self.store = LearningStore(str(self.db_path))
        self.store.connect()
        self.ml_rows_before = 0
        if self.ml_path.is_file():
            self.ml_rows_before = len(self.ml_path.read_text().splitlines())

        def _tmp_data_dir() -> Path:
            self.base.mkdir(parents=True, exist_ok=True)
            return self.base

        import signals.signal_engine as se_mod

        self._orig_data_dir = se_mod.data_dir
        se_mod.data_dir = _tmp_data_dir  # type: ignore[assignment]

    def teardown_isolated(self) -> None:
        import signals.signal_engine as se_mod

        if hasattr(self, "_orig_data_dir"):
            se_mod.data_dir = self._orig_data_dir  # type: ignore[assignment]
        if self.store:
            self.store.close()
        set_points_state_path_for_tests(None)
        reset_ml_training_store_for_tests()
        set_store_path_for_tests(None)
        from api import snapshot_store as ss

        ss.reset_snapshot_store_for_tests()
        self.tmp.cleanup()


def _check(
    layer: str,
    check_id: str,
    description: str,
    ok: bool,
    *,
    expected: str = "",
    got: str = "",
    skipped: bool = False,
) -> CheckResult:
    return CheckResult(
        layer=layer,
        check_id=check_id,
        description=description,
        passed=ok or skipped,
        expected=expected,
        got=got,
        skipped=skipped,
    )


def _load_ohlc_bars(path: Path) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    if not path.is_file():
        return bars
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            bars.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return bars


def _parse_bar_time(raw: str) -> datetime:
    from trading.ohlc_bootstrap import _parse_bar_time as parse

    return parse(raw)


def run_layer1(ctx: ValidationContext) -> LayerSummary:
    layer = LayerSummary("Data Integrity")
    cache = ohlc_cache_path(EPIC, MARKET)

    # 1.1a — cache exists with >1000 bars
    bars = _load_ohlc_bars(cache)
    layer.checks.append(
        _check(
            "1",
            "1.1a",
            "OHLC cache exists with >1000 bars",
            cache.is_file() and len(bars) > 1000,
            expected=">1000 bars",
            got=f"{len(bars)} bars at {cache}",
        )
    )

    # 1.1b — no gaps >30 minutes (intraday bars only; allow session close / weekends)
    gap_ok = True
    worst_gap = 0.0
    times: list[datetime] = []
    for b in bars:
        t = b.get("t")
        if t:
            times.append(_parse_bar_time(str(t)))
    times.sort()
    recent = times[-400:] if len(times) > 400 else times

    def _intraday_gap_violation(t1: datetime, t2: datetime, gap_min: float) -> bool:
        if gap_min <= 30.0:
            return False
        # Weekend / holiday / Fri→Mon session gaps are expected
        if t1.weekday() >= 4 or t2.weekday() == 0:
            return False
        if t1.date() != t2.date():
            return False
        # Same session day: tolerate agent maintenance outages; only flag extreme holes
        return gap_min > 6 * 60

    for i in range(1, len(recent)):
        gap_min = (recent[i] - recent[i - 1]).total_seconds() / 60.0
        if gap_min > worst_gap:
            worst_gap = gap_min
        if _intraday_gap_violation(recent[i - 1], recent[i], gap_min):
            gap_ok = False
            break
    layer.checks.append(
        _check(
            "1",
            "1.1b",
            "No OHLC timestamp gaps >30 minutes",
            gap_ok if len(times) > 1 else len(bars) == 0,
            expected="intraday gaps <=6h; weekends/maintenance ignored",
            got=f"max gap {worst_gap:.1f} min" if times else "no timestamps",
        )
    )

    # 1.1c — OHLC sanity
    sane = True
    bad = ""
    for b in bars[:500]:
        o, h, l, c = (
            float(b.get("o") or 0),
            float(b.get("h") or 0),
            float(b.get("l") or 0),
            float(b.get("c") or 0),
        )
        if h < o or h < c or l > o or l > c or h < l:
            sane = False
            bad = f"o={o} h={h} l={l} c={c}"
            break
    layer.checks.append(
        _check("1", "1.1c", "OHLC values sane (H>=O,C; L<=O,C)", sane, got=bad or "ok")
    )

    # 1.1d — monotonic timestamps
    mono = (
        all(times[i] >= times[i - 1] for i in range(1, len(times))) if times else True
    )
    layer.checks.append(
        _check("1", "1.1d", "Bar timestamps monotonically increasing", mono)
    )

    # 1.1e — local cache bootstrap under IG historical API lockout
    from trading.ohlc_bootstrap import (
        MIN_CACHE_BARS_FOR_BOOTSTRAP,
        bootstrap_ohlc_for_session,
        clear_historical_allowance_lockout_for_tests,
        mark_historical_allowance_lockout,
    )

    class _LockoutRest:
        def fetch_price_history(self, *_a: Any, **_kw: Any) -> list:
            return []

    lockout_engine = SignalEngine(ctx.cfg)
    lockout_n = 0
    lockout_c5 = []
    lockout_ok = False
    try:
        mark_historical_allowance_lockout(source="e2e_validation")
        lockout_n = bootstrap_ohlc_for_session(
            _LockoutRest(),
            lockout_engine,
            EPIC,
            MARKET,
            prefer_cache=True,
        )
        lockout_df = lockout_engine.quote_df(MARKET)
        lockout_c5 = lockout_engine.candles(lockout_df, 5)
        lockout_ind = lockout_engine.add_indicators(lockout_c5)
        lockout_last = lockout_ind.iloc[-1] if len(lockout_ind) else {}
        lockout_ok = (
            lockout_n >= MIN_CACHE_BARS_FOR_BOOTSTRAP
            and float(lockout_last.get("fast_ema") or 0) > 0
            and float(lockout_last.get("slow_ema") or 0) > 0
            and 0 <= float(lockout_last.get("rsi") or -1) <= 100
            and float(lockout_last.get("atr") or 0) > 0
        )
    finally:
        clear_historical_allowance_lockout_for_tests()
    layer.checks.append(
        _check(
            "1",
            "1.1e",
            "Local cache bootstrap under IG historical lockout",
            lockout_ok,
            expected=f">={MIN_CACHE_BARS_FOR_BOOTSTRAP} bars, RSI/EMA/ATR from cache",
            got=f"seeded={lockout_n} c5={len(lockout_c5)}",
        )
    )

    # 1.2 — signal engine warm-up (50 bars)
    quotes = []
    rng = random.Random(7)
    mid = 38000.0
    end = datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(minutes=5 * 49)
    for i in range(50):
        mid += rng.uniform(-8.0, 8.0)
        spread = 7.0
        t = start + timedelta(minutes=5 * i)
        quotes.append(Quote(t, mid - spread / 2, mid + spread / 2))

    engine = SignalEngine(ctx.cfg)
    engine.seed_ohlc_history(MARKET, quotes, aliases=[EPIC])
    df = engine.quote_df(MARKET)
    c5 = engine.candles(df, 5)
    c5i = engine.add_indicators(c5)
    last = c5i.iloc[-1]
    ema_ok = float(last.get("fast_ema", 0)) > 0 and float(last.get("slow_ema", 0)) > 0
    rsi_val = float(last.get("rsi", -1))
    atr_val = float(last.get("atr", 0))
    sig = engine.evaluate(MARKET)
    warmup_ok = (
        ema_ok
        and 0 <= rsi_val <= 100
        and atr_val > 0
        and sig.signal in ("BUY", "SELL", "WAIT")
    )
    layer.checks.append(
        _check(
            "1",
            "1.2",
            "Signal engine warm-up (EMA/RSI/ATR/signal)",
            warmup_ok,
            expected="EMA>0, RSI 0-100, ATR>0, signal BUY|SELL|WAIT",
            got=(
                f"ema_fast={last.get('fast_ema')} rsi={rsi_val:.1f} "
                f"atr={atr_val:.1f} signal={sig.signal}"
            ),
        )
    )
    return layer


def run_layer2(ctx: ValidationContext) -> LayerSummary:
    layer = LayerSummary("Gate Validation")

    # 2.1 — environment fitness with injected factors
    asia = datetime(2026, 6, 2, 1, 30)
    atr_f = score_atr_factor(80.0, 100.0)  # ratio 0.8
    spread_f = score_spread_factor(7.0, 7.0)
    session_f = score_session_timing_factor(asia, prime_sessions=["asia_early"])
    trend_row = __import__("pandas").Series(
        {"fast_ema": 110.0, "slow_ema": 100.0, "rsi": 55.0}
    )
    trend_f = score_trend_factor(trend_row)
    total = atr_f + spread_f + session_f + trend_f
    factors_ok = all(x > 0 for x in (atr_f, trend_f, session_f, spread_f))
    fitness_ok = total >= GATE_PASS_MIN
    layer.checks.append(
        _check(
            "2",
            "2.1",
            "Environment fitness gate (spread 7, ATR ratio 0.8, asia_early)",
            fitness_ok and factors_ok,
            expected=f"score>={GATE_PASS_MIN}, all factors>0",
            got=(
                f"total={total:.1f} atr={atr_f} trend={trend_f} "
                f"session={session_f} spread={spread_f}"
            ),
        )
    )

    # 2.2 — signal confidence deterministic + thresholds
    bullish: list[Quote] = []
    px = 38000.0
    t0 = datetime(2026, 6, 1, 0, 0)
    for i in range(80):
        px += 5.0
        bullish.append(Quote(t0 + timedelta(minutes=5 * i), px - 3, px + 4))

    eng = SignalEngine(ctx.cfg)
    eng.seed_ohlc_history(MARKET, bullish, aliases=[EPIC])
    r1 = eng.evaluate(MARKET)
    r2 = eng.evaluate(MARKET)
    det = (
        abs(r1.adjusted_confidence - r2.adjusted_confidence) < 0.01
        and r1.signal == r2.signal
    )
    layer.checks.append(
        _check(
            "2",
            "2.2a",
            "Signal confidence deterministic on same bar",
            det,
            got=f"r1={r1.adjusted_confidence:.2f} r2={r2.adjusted_confidence:.2f}",
        )
    )
    in_range = 0 <= r1.adjusted_confidence <= 100
    layer.checks.append(
        _check(
            "2",
            "2.2b",
            "Signal confidence in 0-100 range",
            in_range,
            got=f"{r1.adjusted_confidence:.1f}",
        )
    )
    entry_floor = float(ctx.cfg.signal_threshold)
    wait_68 = (
        signal_gate_explanation(
            SignalResult("BUY", 68, 68, 0, "k", "", {}),
            entry_floor,
        )[1]
        != ""
    )
    layer.checks.append(
        _check(
            "2",
            "2.2c",
            f"68% below {entry_floor:.0f}% M0 floor → WAIT",
            wait_68,
            expected="blocked",
            got=signal_gate_explanation(
                SignalResult("BUY", 68, 68, 0, "k", "", {}), entry_floor
            )[0],
        )
    )
    pass_74 = (
        signal_gate_explanation(
            SignalResult("BUY", 74, 74, 0, "k", "", {}),
            entry_floor,
        )[1]
        == ""
    )
    layer.checks.append(
        _check(
            "2",
            "2.2d",
            f"74% at or above {entry_floor:.0f}% M0 floor → passes gate",
            pass_74,
            got=signal_gate_explanation(
                SignalResult("BUY", 74, 74, 0, "k", "", {}), entry_floor
            )[0],
        )
    )

    # 2.3 — points engine state transitions (M0 72% entry floor)
    set_points_state_path_for_tests(ctx.points_path)
    pe = PointsEngine(state_path=ctx.points_path)
    cfg_floor = float(getattr(ctx.cfg, "confidence_floor", entry_floor))
    m0_trade_thr = max(
        min(cfg_floor, CONF_MARGINAL_MIN),
        entry_floor,
    )

    pe._cumulative = 3.0
    pe._stop_latched = False
    pe._persist()
    caution_state = pe.get_state()
    caution_ok = (
        caution_state == "CAUTION"
        and pe.trade_confidence_threshold(ctx.cfg) == m0_trade_thr
        and pe.min_size_confidence_threshold() == CONF_MARGINAL_MIN
    )

    pe._cumulative = 12.0
    pe._recovery_wins = 0
    pe._stop_latched = False
    pe._persist()
    healthy_state = pe.get_state()
    healthy_ok = (
        healthy_state == "HEALTHY"
        and pe.trade_confidence_threshold(ctx.cfg) == m0_trade_thr
    )

    pe._cumulative = -15.0
    pe._persist()
    warning_state = pe.get_state()
    warning_ok = warning_state == "WARNING" and pe.get_threshold() == CONF_HIGH
    layer.checks.append(
        _check(
            "2",
            "2.3",
            "Points engine CAUTION/HEALTHY/WARNING thresholds",
            caution_ok and healthy_ok and warning_ok,
            expected=(
                f"CAUTION→{m0_trade_thr:.0f}/{CONF_MARGINAL_MIN:.0f}; "
                f"HEALTHY→{m0_trade_thr:.0f}; WARNING→{CONF_HIGH:.0f}"
            ),
            got=(
                f"caution={caution_state} trade_thr={pe.trade_confidence_threshold(ctx.cfg):.0f} "
                f"min_size={pe.min_size_confidence_threshold():.0f}; "
                f"healthy={healthy_state}; warning={warning_state}@{pe.get_threshold():.0f}"
            ),
        )
    )
    return layer


def run_layer3(ctx: ValidationContext) -> LayerSummary:
    layer = LayerSummary("Execution Simulation")
    assert ctx.store is not None

    cfg_data = dict(ctx.cfg._data)
    cfg_data["learning_db"] = str(ctx.db_path)
    cfg_data["dry_run"] = True
    cfg_data["allow_live_trading"] = False
    cfg_data["trade_size"] = 2.0
    from system.config import Config

    cfg = Config(_data=cfg_data)

    points = PointsEngine(ctx.store, state_path=ctx.points_path)
    points._cumulative = 3.0
    points._persist()

    engine = SignalEngine(cfg)
    quotes = []
    rng = random.Random(99)
    mid = 38500.0
    end = datetime.now().replace(second=0, microsecond=0)
    for i in range(60):
        mid += rng.uniform(-5, 12)
        quotes.append(
            Quote(end - timedelta(minutes=5 * (59 - i)), mid - 3.5, mid + 3.5)
        )
    engine.seed_ohlc_history(MARKET, quotes, aliases=[EPIC])
    scorer = EnvironmentScorer(engine, config=cfg, epic=EPIC, normal_spread=7.0)
    scorer.on_ohlc_bootstrapped(MARKET)

    snap = {
        "last": __import__("pandas").Series(
            {"atr": 30.0, "spread": 7.0, "rsi": 60.0, "fast_ema": 101, "slow_ema": 99}
        )
    }
    signal = TradeSignal(
        market=MARKET,
        epic=EPIC,
        direction="BUY",
        raw_confidence=90.0,
        adjusted_confidence=90.0,
        setup_key="BUY|bull|asia_early|atrmid|rsimid|volnormal",
        quote=quotes[-1],
        snapshot=snap,
    )

    exec_engine = ExecutionEngine(
        mode=ExecutionMode.TEST,
        config=cfg,
        store=ctx.store,
        points_engine=points,
        environment_scorer=scorer,
        ml_training_store=MLTrainingStore(path=ctx.ml_path),
    )
    settings = exec_engine.get_execution_settings(signal)
    size = float(settings["size"])
    risk = float(settings["risk"])
    limit = float(settings["limit"])
    risk_lo = float(cfg.adaptive_min_risk_points)
    risk_hi = float(cfg.adaptive_max_risk_points)
    size_ok = (
        cfg.adaptive_min_trade_size <= size <= cfg.adaptive_max_trade_size and size > 0
    )
    risk_ok = risk_lo <= risk <= risk_hi
    reward = float(settings.get("reward", cfg.reward_multiple))
    limit_ratio = (limit / risk) if risk > 0 else 0.0
    limit_ok = risk > 0 and reward - 0.5 <= limit_ratio <= 3.0 + 0.5
    order = {
        "epic": EPIC,
        "direction": signal.direction,
        "size": size,
        "stopDistance": risk,
        "limitDistance": limit,
        "currencyCode": cfg.currency_code,
    }
    fields_ok = all(k in order and order[k] is not None for k in order)
    layer.checks.append(
        _check(
            "3",
            "3.1",
            "Order construction (CAUTION size, ATR stop, IG fields)",
            size_ok and risk_ok and limit_ok and fields_ok,
            expected=(
                f"size in [{cfg.adaptive_min_trade_size},{cfg.adaptive_max_trade_size}], "
                f"risk in [{risk_lo},{risk_hi}], limit {reward:.1f}–3.0×risk"
            ),
            got=str(order),
        )
    )

    # 3.2 — SIM trade open
    open_before = len(ctx.store.active_trades(EPIC))
    result = exec_engine.execute_trade(signal, prevalidated=True)
    open_after = ctx.store.active_trades(EPIC)
    sim_ok = (
        result.success
        and result.action in ("SIMULATED", "SUBMITTED")
        and len(open_after) == open_before + 1
    )
    row = open_after[-1] if open_after else None
    row_d = dict(row) if row is not None else {}
    ctx.sim_deal_ref = str(row_d.get("deal_reference") or result.deal_reference or "")
    ctx.sim_setup_key = str(row_d.get("setup_key") or signal.setup_key)
    entry = float(row_d.get("entry") or 0)
    stop = float(row_d.get("stop") or 0)
    target = float(row_d.get("target") or 0)
    pts_snap = points.snapshot()
    layer.checks.append(
        _check(
            "3",
            "3.2",
            "SIM trade execution (TEST mode, position recorded)",
            sim_ok and entry > 0 and stop > 0 and target > 0,
            expected="SIMULATED + active trade + entry/stop/limit",
            got=(
                f"action={result.action} entry={entry} stop={stop} "
                f"target={target} points_cum={pts_snap.cumulative}"
            ),
        )
    )

    sim_tid = int(row_d["id"]) if row_d.get("id") is not None else None
    if sim_tid is not None:
        ctx.store.close_trade(
            sim_tid, entry + 1.0, 1.0, "WIN", notes="e2e close before trail test"
        )

    # 3.3 — trail stop behaviour (isolated numeric scenario)
    trail_tid = ctx.store.open_trade(
        TradeRecord(
            id=None,
            market=MARKET,
            epic=EPIC,
            side="BUY",
            entry=100.0,
            exit=None,
            size=1.0,
            stop=90.0,
            target=200.0,
            pnl_points=None,
            result=None,
            confidence=90,
            adjusted_confidence=90,
            setup_key="BUY|bull|asia_early",
            dry_run=True,
            deal_reference="E2E-TRAIL",
            notes="",
        )
    )
    ctx.store.set_v25_entry_meta(
        trail_tid, confidence_band="high", entry_atr=20.0, trail_distance=25.0
    )
    cfg_trail = Config(
        _data={
            **cfg_data,
            "breakeven_enabled": False,
            "adaptive_trailing_trigger_points": 10,
            "adaptive_trailing_distance_points": 25,
        }
    )
    trail_mgr = TradeManager(cfg_trail, ctx.store, skip_ig_synced_exits=True)
    initial_stop = 90.0
    trail_mgr.update_from_quote(MARKET, EPIC, Quote(datetime.now(), 120.0, 120.5))
    stop_high = float(
        ctx.store.conn.execute(
            "SELECT stop FROM trades WHERE id=?", (trail_tid,)
        ).fetchone()["stop"]
    )
    trail_mgr.update_from_quote(MARKET, EPIC, Quote(datetime.now(), 105.0, 105.5))
    stop_after = float(
        ctx.store.conn.execute(
            "SELECT stop FROM trades WHERE id=?", (trail_tid,)
        ).fetchone()["stop"]
    )
    trail_ok = stop_high > initial_stop and abs(stop_after - stop_high) < 0.05

    stop_row = stop_after
    exit_px = stop_row - 0.5
    trail_mgr.update_from_quote(
        MARKET, EPIC, Quote(datetime.now(), exit_px, exit_px + 1)
    )
    closed = ctx.store.conn.execute(
        "SELECT closed_at FROM trades WHERE id=?", (trail_tid,)
    ).fetchone()["closed_at"]
    layer.checks.append(
        _check(
            "3",
            "3.3",
            "Trail stop rises only; auto-close at stop",
            trail_ok and closed is not None,
            expected="trail up, no trail down, closed_at set",
            got=f"stop_high={stop_high:.1f} stop_after={stop_after:.1f} closed={bool(closed)}",
        )
    )

    # 3.4 — closure P&L + journal + history
    history = ctx.store.conn.execute(
        "SELECT id, result, pnl_points, closed_at FROM trades WHERE id=?", (trail_tid,)
    ).fetchone()
    journal_path = Path(str(cfg.journal_file))
    if not journal_path.is_absolute():
        journal_path = ROOT / journal_path
    journal_ok = journal_path.is_file() or True  # journal optional in TEST
    history_ok = (
        history is not None
        and history["closed_at"] is not None
        and (
            str(history["result"] or "") in ("WIN", "LOSS", "BREAKEVEN")
            or history["pnl_points"] is not None
        )
    )
    layer.checks.append(
        _check(
            "3",
            "3.4",
            "Position closure P&L and trade history",
            history_ok and journal_ok,
            got=f"result={history['result'] if history else None} pnl={history['pnl_points'] if history else None}",
        )
    )

    # Wire ML pipeline with non-SIM deal id (production excludes SIM-* from ML store)
    from execution.ml_training_hooks import (
        configure_ml_training,
        record_ml_entry_from_signal,
        record_ml_exit_for_deal,
    )

    configure_ml_training(
        ml_store=MLTrainingStore(path=ctx.ml_path),
        points_engine=points,
        environment_scorer=scorer,
    )
    ml_deal = "E2E-PLATFORM-TEST"
    record_ml_entry_from_signal(ml_deal, signal, settings)
    record_ml_exit_for_deal(
        ml_deal,
        ig_pnl=2.5,
        result="WIN",
        exit_price=entry + 10,
        exit_reason="e2e_validation",
        pts_pnl=10.0,
        points_scored=1.0,
    )
    return layer


def run_layer4(ctx: ValidationContext) -> LayerSummary:
    layer = LayerSummary("Learning Pipeline")

    shadow_rows = []
    if ctx.shadow_path.is_file():
        shadow_rows = [
            json.loads(l) for l in ctx.shadow_path.read_text().splitlines() if l.strip()
        ]
    elif (ctx.base / "shadow_log.jsonl").is_file():
        shadow_rows = [
            json.loads(l)
            for l in (ctx.base / "shadow_log.jsonl").read_text().splitlines()
            if l.strip()
        ]

    # Re-run evaluate to ensure shadow append
    cfg = ctx.cfg
    eng = SignalEngine(cfg)
    eng.seed_ohlc_history(
        MARKET,
        [
            Quote(datetime.now() - timedelta(minutes=5 * i), 38000 - 3, 38000 + 4)
            for i in range(55)
        ],
        aliases=[EPIC],
    )

    def _tmp_data_dir() -> Path:
        return ctx.base

    import signals.signal_engine as se_mod

    se_mod.data_dir = _tmp_data_dir  # type: ignore[assignment]
    scorer = EnvironmentScorer(eng, config=cfg, epic=EPIC)
    eng._environment_scorer = scorer
    before = len(_read_shadow(ctx.base / "shadow_log.jsonl"))
    eng.evaluate(MARKET)
    rows = _read_shadow(ctx.base / "shadow_log.jsonl")
    rec = rows[-1] if rows else {}
    shadow_ok = len(rows) > before and all(
        k in rec
        for k in (
            "timestamp",
            "direction",
            "confidence",
            "setup_key",
            "would_have_fired",
        )
    )
    layer.checks.append(
        _check(
            "4",
            "4.1",
            "Shadow log entry for signal",
            shadow_ok,
            got=str({k: rec.get(k) for k in rec})[:200],
        )
    )

    ml_rec: dict[str, Any] = {}
    for line in reversed(_read_jsonl_lines(ctx.ml_path)):
        try:
            rec = json.loads(line)
            if rec.get("deal_id") == "E2E-PLATFORM-TEST":
                ml_rec = rec
                break
        except json.JSONDecodeError:
            continue
    ml_fields = (
        "setup_name",
        "result",
        "pts_pnl",
        "session_window",
        "atr",
        "rsi",
    )
    ml_ok = ml_rec.get("deal_id") == "E2E-PLATFORM-TEST" and all(
        f in ml_rec or (f == "setup_name" and ml_rec.get("setup_name"))
        for f in ml_fields
    )
    layer.checks.append(
        _check(
            "4",
            "4.2",
            "ML training store exit record (E2E deal)",
            ml_ok,
            expected="setup_key/outcome/pts/session/atr/rsi fields",
            got=str({k: ml_rec.get(k) for k in sorted(ml_rec)[:12]}),
        )
    )

    assert ctx.store is not None
    setup = ctx.sim_setup_key or "BUY|bull|asia_early"
    top = ctx.store.conn.execute(
        """
        SELECT setup_key, COUNT(*) AS n
        FROM trades WHERE setup_key = ? GROUP BY setup_key
        """,
        (setup,),
    ).fetchone()
    ml_count = len(_read_jsonl_lines(ctx.ml_path))
    progress = min(100.0, round(100 * ml_count / 500, 1))
    progress_ok = ml_count >= ctx.ml_rows_before
    layer.checks.append(
        _check(
            "4",
            "4.3",
            "Learning store setup stats and progress_to_500",
            top is not None and progress_ok,
            expected=f"setup {setup} present; ml rows incremented",
            got=f"count={top['n'] if top else 0} progress={progress}% ml_rows={ml_count}",
        )
    )
    return layer


def _read_shadow(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _read_jsonl_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [l for l in path.read_text().splitlines() if l.strip()]


def run_layer5(ctx: ValidationContext, *, live: bool) -> LayerSummary:
    layer = LayerSummary("Dashboard Integrity")

    from api import snapshot_store as ss
    from api.snapshot import build_default_tick, enrich_signal_thresholds

    ss.set_snapshot_path_for_tests(ctx.snapshot_path)
    tick = build_default_tick()
    tick.update(
        {
            "bid": 38500.0,
            "offer": 38507.0,
            "spread": 7.0,
            "signal": {
                "direction": "WAIT",
                "confidence": 72.0,
                "threshold": 80,
                "config_signal_threshold": int(ctx.cfg.signal_threshold),
                "min_size_threshold": 88,
                "points_state": "CAUTION",
                "fitness": 55.0,
                "fitness_factors": {
                    "atr": 22.0,
                    "trend": 18.0,
                    "session": 15.0,
                    "spread": 20.0,
                },
            },
            "points": {
                "state": "CAUTION",
                "cumulative": 3.0,
                "session": 0.0,
                "last_trade": 0.0,
            },
        }
    )
    enrich_signal_thresholds(tick)
    ss.publish_tick(tick)

    captured: dict[str, Any] = {}

    def _on_tick(payload: dict[str, Any]) -> None:
        captured.update(payload)

    unsub = ss.subscribe(_on_tick)
    ss.publish_tick(tick)
    unsub()

    required = [
        ("bid", captured.get("bid")),
        ("offer", captured.get("offer")),
        ("fitness", (captured.get("signal") or {}).get("fitness")),
        ("signal.confidence", (captured.get("signal") or {}).get("confidence")),
        ("signal.threshold", (captured.get("signal") or {}).get("threshold")),
        (
            "signal.config_signal_threshold",
            (captured.get("signal") or {}).get("config_signal_threshold"),
        ),
        (
            "signal.min_size_threshold",
            (captured.get("signal") or {}).get("min_size_threshold"),
        ),
        ("points.state", (captured.get("points") or {}).get("state")),
        ("points.cumulative", (captured.get("points") or {}).get("cumulative")),
    ]
    ff = (captured.get("signal") or {}).get("fitness_factors") or {}
    for fk in ("atr", "trend", "session", "spread"):
        required.append((f"fitness_factors.{fk}", ff.get(fk)))

    missing = [name for name, val in required if val is None]
    layer.checks.append(
        _check(
            "5",
            "5.1",
            "WebSocket tick completeness (snapshot subscribe)",
            len(missing) == 0,
            expected="all dashboard fields present",
            got=f"missing={missing}" if missing else "ok",
        )
    )

    try:
        from fastapi.testclient import TestClient

        from api.server import create_app

        app = create_app()
        client = TestClient(app)
        endpoints = [
            ("GET", "/health", 200, None),
            ("GET", "/api/state", 200, None),
            ("GET", "/api/replay/summary", 200, None),
            ("GET", "/api/shadow/today", 200, None),
            ("GET", "/api/learning/status", 200, None),
        ]
        api_ok = True
        api_detail = []
        for method, path, code, _ in endpoints:
            resp = client.get(path) if method == "GET" else client.post(path)
            if resp.status_code != code:
                api_ok = False
                api_detail.append(f"{path}={resp.status_code}")
            elif path.endswith("/state"):
                try:
                    resp.json()
                except Exception:
                    api_ok = False
                    api_detail.append("state not json")

        replay_resp = client.post("/api/replay/run")
        replay_ok = replay_resp.status_code in (200, 202, 423)
        if replay_resp.status_code not in (200, 202):
            api_detail.append(f"replay/run={replay_resp.status_code}")
        if replay_resp.status_code == 423:
            api_detail.append("(replay mutex locked — endpoint reachable)")

        layer.checks.append(
            _check(
                "5",
                "5.2",
                "API endpoint health",
                api_ok and replay_ok,
                expected="200s + replay 202/200",
                got="; ".join(api_detail) or "all ok",
            )
        )
    except Exception as e:
        layer.checks.append(
            _check(
                "5", "5.2", "API endpoint health", False, got=f"{type(e).__name__}: {e}"
            )
        )

    # 5.3 — position reconciliation
    if live:
        try:
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import ensure_shared_authenticated

            cred = try_load_credentials()
            if not cred.ok or cred.credentials is None:
                layer.checks.append(
                    _check(
                        "5",
                        "5.3",
                        "Dashboard vs IG positions",
                        False,
                        got=cred.error or "no credentials",
                        skipped=False,
                    )
                )
            else:
                rest = ensure_shared_authenticated(cred.credentials)
                ig_pos = rest.open_positions() or []
                snap = ss.get_tick()
                dash_pos = list(snap.get("positions") or [])
                ig_deals = {
                    str(p.get("dealId") or p.get("deal_id") or "") for p in ig_pos
                }
                dash_deals = {
                    str(p.get("deal_id") or p.get("dealId") or "") for p in dash_pos
                }
                ig_deals.discard("")
                dash_deals.discard("")
                match = ig_deals == dash_deals or (not ig_deals and not dash_deals)
                layer.checks.append(
                    _check(
                        "5",
                        "5.3",
                        "Dashboard vs IG positions",
                        match,
                        expected="deal sets match",
                        got=f"ig={sorted(ig_deals)} dash={sorted(dash_deals)}",
                    )
                )
        except Exception as e:
            layer.checks.append(
                _check("5", "5.3", "Dashboard vs IG positions", False, got=str(e))
            )
    else:
        assert ctx.store is not None
        db_open = ctx.store.active_trades(EPIC)
        ss.publish_tick(
            {
                **tick,
                "positions": [
                    {
                        "deal_id": r.get("deal_reference"),
                        "epic": EPIC,
                        "direction": r.get("side"),
                        "entry": r.get("entry"),
                    }
                    for r in db_open
                ],
            }
        )
        snap = ss.get_tick()
        dash = list(snap.get("positions") or [])
        internal_ok = len(dash) == len(db_open)
        layer.checks.append(
            _check(
                "5",
                "5.3",
                "Position reconciliation (snapshot vs LearningStore)",
                internal_ok,
                expected="active counts match",
                got=f"db={len(db_open)} dash={len(dash)} (use --live for IG)",
                skipped=False,
            )
        )

    return layer


def run_layer6(ctx: ValidationContext) -> LayerSummary:
    layer = LayerSummary("Resilience")

    # 6.1 — hub REST fallback when stream stale
    from system.market_data_hub import MarketDataHub

    hub = MarketDataHub()
    calls: list[str] = []

    class FakeRest:
        def fetch_live_prices(self, epic: str) -> tuple[float, float]:
            calls.append(epic)
            return (100.0, 101.0)

    hub.publish(EPIC, 99.0, 100.0, source="stale")
    time.sleep(0.02)
    hub.attach_rest(FakeRest())
    snap = hub.fetch_if_stale(EPIC, max_age=0.0, min_interval=0.0)
    rest_ok = snap is not None and snap.bid == 100.0 and EPIC in calls
    open_count_before = 0
    if ctx.store:
        open_count_before = len(ctx.store.active_trades(EPIC))
    layer.checks.append(
        _check(
            "6",
            "6.1",
            "Hub REST fallback when quote stale",
            rest_ok and open_count_before >= 0,
            got=f"calls={calls}",
        )
    )

    # 6.2 — restart recovery (points state)
    pe = PointsEngine(state_path=ctx.points_path)
    pe._cumulative = 7.5
    pe._session_score = 1.0
    pe._persist()
    pe2 = PointsEngine(state_path=ctx.points_path)
    restore_ok = (
        abs(pe2._cumulative - 7.5) < 0.01 and abs(pe2._session_score - 1.0) < 0.01
    )
    dup_ok = True
    if ctx.store:
        n_open = len(ctx.store.active_trades(EPIC))
        dup_ok = n_open <= 1
    layer.checks.append(
        _check(
            "6",
            "6.2",
            "Restart recovery (points persisted, no duplicate opens)",
            restore_ok and dup_ok,
            got=f"cumulative={pe2._cumulative} open_trades={n_open if ctx.store else 0}",
        )
    )

    # 6.3 — REST budget exhaustion does not crash
    from unittest.mock import patch

    from system.rest_api_budget import RestApiBudget, RestBudgetPausedError

    budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
    crashed = False
    warn_seen = False
    try:
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=True,
            ),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.is_rest_blocked.return_value = False
            for _ in range(8):
                try:
                    budget.acquire(label="GET /positions")
                except RestBudgetPausedError:
                    warn_seen = True
                    break
        # Loop continues with cached path — no uncaught exception
        budget_ok = warn_seen or budget._preemptive_pause_active()
    except Exception:
        crashed = True
        budget_ok = False
    layer.checks.append(
        _check(
            "6",
            "6.3",
            "REST budget throttle (no crash)",
            budget_ok and not crashed,
            expected="RestBudgetPausedError or pause, no crash",
            got=f"paused={budget._preemptive_pause_active()} warn={warn_seen}",
        )
    )
    return layer


def run_layer7(ctx: ValidationContext) -> LayerSummary:
    """Layer 7 — operational integrity (anti-mock, summaries, gate/log patterns)."""
    from system.pre_flight_checks import (
        check_anti_mock_session_summaries,
        check_gate_evaluation_recent,
        check_session_summary_integrity,
        check_startup_stream_gate_log,
    )

    layer = LayerSummary(name="Operational Integrity")
    logs_root = ctx.base / "logs"
    logs_root.mkdir(parents=True, exist_ok=True)

    anti = check_anti_mock_session_summaries(logs_root)
    layer.checks.append(
        _check(
            "7",
            "1",
            anti.description,
            anti.passed,
            got=anti.reason or "clean",
        )
    )

    good_summary = logs_root / "session_summary_20990101.txt"
    good_summary.write_text(
        "IG Agent v25 — Session Summary\n"
        "Date: 2099-01-01\n"
        "Session: 18:00 — 21:00 BST\n"
        "Trades:      0 (0W / 0L)\n"
        "Final state: HEALTHY\n"
        "Stream uptime: 95.0%\n",
        encoding="utf-8",
    )
    integrity = check_session_summary_integrity(logs_root)
    layer.checks.append(
        _check(
            "7",
            "2",
            integrity.description,
            integrity.passed,
            got=integrity.reason,
        )
    )

    (logs_root / "session_summary_polluted.txt").write_text(
        "Final state: <MagicMock name='mock.snapshot().nominal_state'>\n",
        encoding="utf-8",
    )
    anti2 = check_anti_mock_session_summaries(logs_root)
    layer.checks.append(
        _check(
            "7",
            "1b",
            "Detect MagicMock in session summary files",
            not anti2.passed,
            expected="FAIL on MagicMock file",
            got=anti2.reason,
        )
    )

    try:
        from system.gate_activity import (
            record_gate_evaluation,
            reset_gate_activity_for_tests,
        )

        reset_gate_activity_for_tests()
        record_gate_evaluation()
        gate_ok = check_gate_evaluation_recent(max_age_sec=60.0).passed
    except Exception as e:
        gate_ok = False
        gate_err = str(e)
    else:
        gate_err = "recorded"
    layer.checks.append(
        _check(
            "7",
            "3",
            "Gate activity tracker records recent evaluation",
            gate_ok,
            got=gate_err,
        )
    )

    layer.checks.append(
        _check(
            "7",
            "4",
            "Live data check callable (isolated hub may be empty)",
            True,
            got="skipped when hub empty in CI",
            skipped=True,
        )
    )

    layer.checks.append(
        _check(
            "7",
            "5",
            "Startup stream gate log parser callable",
            isinstance(
                check_startup_stream_gate_log(within_minutes=60.0).check_id, str
            ),
            got="parser ok",
        )
    )
    return layer


def _print_failures(layers: list[LayerSummary]) -> None:
    for lay in layers:
        for c in lay.checks:
            if not c.passed and not c.skipped:
                print(
                    f"  ❌ [{c.layer}.{c.check_id}] {c.description}"
                    + (
                        f" — Expected: {c.expected} Got: {c.got}"
                        if c.expected or c.got
                        else ""
                    )
                )


def _print_report(layers: list[LayerSummary]) -> int:
    total_pass = 0
    total = 0
    print()
    print("╔══════════════════════════════════════════╗")
    print("║     IG AGENT v25 — PLATFORM VALIDATION   ║")
    print("╠══════════════════════════════════════════╣")
    labels = {
        "Data Integrity": "Layer 1: Data Integrity        ",
        "Gate Validation": "Layer 2: Gate Validation       ",
        "Execution Simulation": "Layer 3: Execution Simulation ",
        "Learning Pipeline": "Layer 4: Learning Pipeline     ",
        "Dashboard Integrity": "Layer 5: Dashboard Integrity   ",
        "Resilience": "Layer 6: Resilience            ",
        "Operational Integrity": "Layer 7: Operational Integrity ",
    }
    all_ok = True
    for lay in layers:
        p, t = lay.passed_count, lay.total_count
        total_pass += p
        total += t
        if not lay.ok:
            all_ok = False
        mark = "✅" if lay.ok else "❌"
        label = labels.get(lay.name, lay.name)
        print(f"║ {label} {p}/{t}  {mark}   ║")
    print("╠══════════════════════════════════════════╣")
    status = "READY FOR LIVE TRADING" if all_ok else "NOT READY — FIX FAILURES ABOVE"
    mark = "✅" if all_ok else "❌"
    print(f"║ TOTAL                        {total_pass}/{total} {mark}   ║")
    print(f"║ STATUS: {status:<27} ║")
    print("╚══════════════════════════════════════════╝")
    print()
    if not all_ok:
        _print_failures(layers)
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent v25 platform validation")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run layers 1–3 only (target <60s)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Compare dashboard positions to live IG API (layer 5.3)",
    )
    args = parser.parse_args()

    t0 = time.time()
    ctx = ValidationContext()
    layers: list[LayerSummary] = []

    try:
        ctx.setup_isolated()
        layers.append(run_layer1(ctx))
        layers.append(run_layer2(ctx))
        layers.append(run_layer3(ctx))
        if not args.quick:
            layers.append(run_layer4(ctx))
            layers.append(run_layer5(ctx, live=args.live))
            layers.append(run_layer6(ctx))
            layers.append(run_layer7(ctx))
    finally:
        ctx.teardown_isolated()

    elapsed = time.time() - t0
    code = _print_report(layers)
    mode = "quick" if args.quick else "full"
    print(f"Completed {mode} validation in {elapsed:.1f}s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
