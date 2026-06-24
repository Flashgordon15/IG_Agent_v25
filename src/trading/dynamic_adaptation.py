"""
Dynamic adaptation — rolling TICK_FIFO lookback, elastic gate floors, benchmark PF feedback.

v31 sandbox evolutionary loop: relaxes entry thresholds in flat regimes (down to 38%),
tightens after poor rolling profit factor, reads shadow benchmark ledger for feedback.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.pnl_math import price_delta_to_ig_points

# Legacy default — superseded at runtime by effective_gate_pass_min(epic).
GATE_PASS_MIN_DEFAULT = 55.0

FLAT_SIGNAL_FLOOR = 38.0
FLAT_FITNESS_FLOOR = 38.0
_LOOKBACK_MIN_SEC = 5 * 60
_LOOKBACK_MAX_SEC = 30 * 60
_DEFAULT_LOOKBACK_SEC = 15 * 60
_FLAT_ATR_RATIO_MAX = 0.55
_REFRESH_TTL_SEC = 4.0
# v31 sandbox — relaxed tick tape barriers (production keeps stricter defaults below)
_SANDBOX_MIN_TICKS = 5
_SANDBOX_LOOKBACK_MIN_SEC = 2 * 60
_PROD_MIN_TICKS_COMPUTE = 3
_PROD_MIN_TICKS_DENSE = 12


@dataclass(frozen=True, slots=True)
class LookbackMetrics:
    epic: str
    window_sec: float
    tick_count: int
    atr_pts: float
    atr_baseline_pts: float
    atr_ratio: float
    price_speed_pts_per_min: float
    flat_regime: bool
    data_sparse: bool


def _is_v31_sandbox() -> bool:
    """True only inside the isolated v31 sandbox workspace."""
    import os

    if os.environ.get("IG_NODE_PROFILE") == "sandbox":
        return True
    try:
        from system.config_loader import get_config

        return str(get_config().get("version") or "").startswith("31")
    except Exception:
        return False


def _min_ticks_for_compute() -> int:
    return _SANDBOX_MIN_TICKS if _is_v31_sandbox() else _PROD_MIN_TICKS_COMPUTE


def _min_ticks_dense() -> int:
    return _SANDBOX_MIN_TICKS if _is_v31_sandbox() else _PROD_MIN_TICKS_DENSE


def _lookback_window_min_sec() -> float:
    return float(_SANDBOX_LOOKBACK_MIN_SEC if _is_v31_sandbox() else _LOOKBACK_MIN_SEC)


@dataclass(frozen=True, slots=True)
class ElasticGates:
    signal_threshold: float
    fitness_min: float
    ml_floor: float
    regime: str
    detail: str


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    trades: int
    wins: int
    losses: int
    net_pnl_gbp: float
    profit_factor: float
    win_rate_pct: float
    source: str = ""


class RollingLookbackLayer:
    """Reads TICK_FIFO + OHLC cache tail to derive short-horizon vol metrics."""

    @staticmethod
    def ticks_in_window(epic: str, *, since_ts: float) -> list[dict[str, Any]]:
        from trading.cache_reaper import volatile_tick_slots_for_epic

        key = str(epic or "").strip()
        return [
            t
            for t in volatile_tick_slots_for_epic(key)
            if float(t.get("ts") or 0) >= since_ts
        ]

    @staticmethod
    def ohlc_atr_baseline_pts(epic: str) -> float:
        try:
            from trading.ohlc_cache_paths import ohlc_cache_path

            path = ohlc_cache_path(epic)
            if not path.is_file():
                return 0.0
            rows: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            tail = rows[-21:]
            if len(tail) < 2:
                return 0.0
            import pandas as pd
            from signals.indicators import atr as atr_fn

            df = pd.DataFrame(tail)
            for col in ("high", "low", "close"):
                if col not in df.columns:
                    return 0.0
            series = atr_fn(df["high"], df["low"], df["close"], period=14)
            last = float(series.iloc[-1])
            return price_delta_to_ig_points(epic, last)
        except Exception:
            return 0.0

    @staticmethod
    def compute(
        epic: str,
        *,
        window_sec: float = _DEFAULT_LOOKBACK_SEC,
        flat_atr_ratio_max: float = _FLAT_ATR_RATIO_MAX,
    ) -> LookbackMetrics:
        key = str(epic or "").strip()
        now = time.time()
        window = max(_lookback_window_min_sec(), min(float(window_sec), _LOOKBACK_MAX_SEC))
        since = now - window
        ticks = RollingLookbackLayer.ticks_in_window(key, since_ts=since)
        min_ticks = _min_ticks_for_compute()

        if len(ticks) < min_ticks:
            return LookbackMetrics(
                epic=key,
                window_sec=window,
                tick_count=len(ticks),
                atr_pts=0.0,
                atr_baseline_pts=0.0,
                atr_ratio=1.0,
                price_speed_pts_per_min=0.0,
                flat_regime=False,
                data_sparse=True,
            )

        mids = [float(t["mid"]) for t in ticks]
        bids = [float(t["bid"]) for t in ticks]
        offers = [float(t["offer"]) for t in ticks]

        trs: list[float] = []
        for i in range(1, len(mids)):
            hl = offers[i] - bids[i]
            hc = abs(offers[i] - mids[i - 1])
            lc = abs(bids[i] - mids[i - 1])
            trs.append(max(hl, hc, lc))

        atr_raw = sum(trs) / max(len(trs), 1)
        atr_pts = price_delta_to_ig_points(key, atr_raw)
        baseline = RollingLookbackLayer.ohlc_atr_baseline_pts(key) or max(atr_pts, 1e-6)
        atr_ratio = atr_pts / baseline if baseline > 0 else 1.0

        dt_min = max(
            (float(ticks[-1]["ts"]) - float(ticks[0]["ts"])) / 60.0,
            1e-6,
        )
        speed_pts = price_delta_to_ig_points(key, abs(mids[-1] - mids[0])) / dt_min

        flat = atr_ratio < flat_atr_ratio_max and speed_pts < (baseline * 0.08)
        # v31 sandbox cold-start: engage 38% elastic floor without full lookback tape
        if _is_v31_sandbox() and atr_ratio <= 1.0:
            flat = True

        return LookbackMetrics(
            epic=key,
            window_sec=window,
            tick_count=len(ticks),
            atr_pts=round(atr_pts, 4),
            atr_baseline_pts=round(baseline, 4),
            atr_ratio=round(atr_ratio, 4),
            price_speed_pts_per_min=round(speed_pts, 4),
            flat_regime=flat,
            data_sparse=len(ticks) < _min_ticks_dense(),
        )


def compute_lookback_metrics(
    epic: str,
    *,
    window_sec: float = _DEFAULT_LOOKBACK_SEC,
) -> LookbackMetrics:
    """Module-level helper — delegates to RollingLookbackLayer."""
    cfg = _adaptation_config()
    return RollingLookbackLayer.compute(
        epic,
        window_sec=float(cfg.get("lookback_sec") or window_sec),
        flat_atr_ratio_max=float(cfg.get("flat_atr_ratio_max") or _FLAT_ATR_RATIO_MAX),
    )


def _adaptation_config() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        block = get_config().get("dynamic_adaptation") or {}
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def adaptation_enabled() -> bool:
    block = _adaptation_config()
    if "enabled" in block:
        return bool(block.get("enabled"))
    try:
        from system.config_loader import get_config

        ver = str(get_config().get("version") or "")
        return ver.startswith("31")
    except Exception:
        return False


def _protective_floor() -> float:
    try:
        from system.protective_learning import signal_threshold_floor

        val = signal_threshold_floor()
        if val is not None and float(val) > 0:
            return float(val)
    except Exception:
        pass
    try:
        from system.config_loader import get_config

        return float(get_config().get("signal_threshold") or 45.0)
    except Exception:
        return 45.0


def _flat_signal_floor() -> float:
    cfg = _adaptation_config()
    try:
        return float(cfg.get("flat_signal_floor") or FLAT_SIGNAL_FLOOR)
    except (TypeError, ValueError):
        return FLAT_SIGNAL_FLOOR


def _flat_fitness_floor() -> float:
    cfg = _adaptation_config()
    try:
        return float(cfg.get("flat_fitness_floor") or FLAT_FITNESS_FLOOR)
    except (TypeError, ValueError):
        return FLAT_FITNESS_FLOOR


def compute_elastic_gates(
    epic: str,
    metrics: LookbackMetrics,
    *,
    base_signal: float,
    base_fitness: float = GATE_PASS_MIN_DEFAULT,
) -> ElasticGates:
    prot = _protective_floor()
    sig = float(base_signal)
    fit = float(base_fitness)

    if metrics.data_sparse:
        return ElasticGates(sig, fit, sig, "sparse", "insufficient tick tape")

    if metrics.flat_regime:
        relax_scale = max(0.0, 1.0 - metrics.atr_ratio)
        flat_sig = _flat_signal_floor()
        flat_fit = _flat_fitness_floor()
        if _is_v31_sandbox() or metrics.atr_ratio < 0.5:
            relax_sig = flat_sig
            relax_fit = flat_fit
        else:
            relax_sig = max(flat_sig, sig - 12.0 * relax_scale)
            relax_fit = max(flat_fit, fit - 17.0 * relax_scale)
        return ElasticGates(
            signal_threshold=round(relax_sig, 2),
            fitness_min=round(relax_fit, 2),
            ml_floor=round(relax_sig, 2),
            regime="flat",
            detail=(
                f"atr_ratio={metrics.atr_ratio:.2f} "
                f"speed={metrics.price_speed_pts_per_min:.2f}/min"
            ),
        )

    if metrics.atr_ratio > 1.8:
        tighten = min(5.0, (metrics.atr_ratio - 1.8) * 4.0)
        return ElasticGates(
            signal_threshold=round(min(62.0, sig + tighten), 2),
            fitness_min=round(min(60.0, fit + tighten), 2),
            ml_floor=round(min(62.0, sig + tighten), 2),
            regime="hot",
            detail=f"atr_ratio={metrics.atr_ratio:.2f}",
        )

    return ElasticGates(sig, fit, sig, "normal", "baseline")


class PerformanceFeedbackLoop:
    """Reads sandbox benchmark ledger + learning store for rolling PF / win rate."""

    @staticmethod
    def _benchmark_path() -> Path:
        try:
            from system.paths import project_root

            return project_root() / "logs" / "v30_vs_v31_benchmark.json"
        except Exception:
            return Path("logs/v30_vs_v31_benchmark.json")

    @staticmethod
    def load_benchmark_snapshot() -> PerformanceSnapshot | None:
        path = PerformanceFeedbackLoop._benchmark_path()
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        api = str(payload.get("api_base") or "")
        if api and ":8081" not in api:
            return None
        sim = payload.get("v31_simulated") or {}
        if not isinstance(sim, dict):
            return None
        trades = int(sim.get("trades") or 0)
        if trades <= 0:
            return None
        return PerformanceSnapshot(
            trades=trades,
            wins=int(sim.get("wins") or 0),
            losses=int(sim.get("losses") or 0),
            net_pnl_gbp=float(sim.get("net_pnl_gbp") or 0.0),
            profit_factor=float(sim.get("profit_factor") or 0.0),
            win_rate_pct=float(sim.get("win_rate_pct") or 0.0),
            source="benchmark_json",
        )

    @staticmethod
    def load_epic_snapshot(epic: str, *, limit: int = 40) -> PerformanceSnapshot:
        key = str(epic or "").strip()
        rows: list[dict[str, Any]] = []
        try:
            from data.learning_store import LearningStore

            store = LearningStore()
            if hasattr(store, "recent_confirmed_closed_trades"):
                rows.extend(store.recent_confirmed_closed_trades(limit=limit))
        except Exception:
            pass

        bench = PerformanceFeedbackLoop.load_benchmark_snapshot()
        wins = losses = 0
        gross_win = gross_loss = 0.0
        net = 0.0

        epic_rows = [r for r in rows if str(r.get("epic") or "") == key]
        if not epic_rows and bench is not None:
            return PerformanceSnapshot(
                trades=bench.trades,
                wins=bench.wins,
                losses=bench.losses,
                net_pnl_gbp=bench.net_pnl_gbp,
                profit_factor=bench.profit_factor,
                win_rate_pct=bench.win_rate_pct,
                source=bench.source,
            )

        for r in epic_rows[-limit:]:
            pnl = float(r.get("pnl_gbp") or r.get("net_pnl_gbp") or 0)
            net += pnl
            if pnl > 0:
                wins += 1
                gross_win += pnl
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)

        n = wins + losses
        if n == 0 and bench is not None:
            return PerformanceSnapshot(
                trades=bench.trades,
                wins=bench.wins,
                losses=bench.losses,
                net_pnl_gbp=bench.net_pnl_gbp,
                profit_factor=bench.profit_factor,
                win_rate_pct=bench.win_rate_pct,
                source=f"{bench.source}+epic_fallback",
            )

        pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
        wr = (100.0 * wins / n) if n else 0.0
        return PerformanceSnapshot(
            trades=n,
            wins=wins,
            losses=losses,
            net_pnl_gbp=round(net, 2),
            profit_factor=round(pf, 3),
            win_rate_pct=round(wr, 1),
            source="learning_store" if epic_rows else "none",
        )

    @staticmethod
    def apply_bias(
        gates: ElasticGates,
        perf: PerformanceSnapshot,
        *,
        min_trades: int | None = None,
    ) -> ElasticGates:
        cfg = _adaptation_config()
        try:
            floor_trades = int(
                min_trades if min_trades is not None else cfg.get("performance_min_trades") or 8
            )
        except (TypeError, ValueError):
            floor_trades = 8

        if perf.trades < floor_trades:
            return gates

        sig, fit = gates.signal_threshold, gates.fitness_min
        prot = _protective_floor()

        try:
            pf_tighten = float(cfg.get("pf_tighten_below") or 1.0)
            pf_loosen = float(cfg.get("pf_loosen_above") or 1.6)
        except (TypeError, ValueError):
            pf_tighten, pf_loosen = 1.0, 1.6

        if perf.profit_factor < pf_tighten or perf.net_pnl_gbp < 0:
            delta = min(8.0, 2.0 + max(0.0, 1.0 - perf.profit_factor) * 5.0)
            sig = min(62.0, sig + delta)
            fit = min(60.0, fit + delta)
            regime = f"{gates.regime}+tighten"
        elif perf.profit_factor >= pf_loosen and perf.win_rate_pct >= 55.0:
            delta = min(4.0, (perf.profit_factor - 1.0) * 2.0)
            sig = max(prot, sig - delta)
            fit = max(prot, fit - delta)
            regime = f"{gates.regime}+loosen"
        else:
            return gates

        return ElasticGates(
            signal_threshold=round(sig, 2),
            fitness_min=round(fit, 2),
            ml_floor=round(sig, 2),
            regime=regime,
            detail=f"{gates.detail}; pf={perf.profit_factor} wr={perf.win_rate_pct}%",
        )


class AdaptationRegistry:
    """Thread-safe per-epic runtime gate overlay."""

    _lock = threading.RLock()
    _gates: dict[str, ElasticGates] = {}
    _metrics: dict[str, LookbackMetrics] = {}
    _performance: dict[str, PerformanceSnapshot] = {}
    _last_refresh: dict[str, float] = {}

    @classmethod
    def update(
        cls,
        epic: str,
        metrics: LookbackMetrics,
        gates: ElasticGates,
        perf: PerformanceSnapshot,
    ) -> None:
        key = str(epic or "").strip()
        with cls._lock:
            cls._metrics[key] = metrics
            cls._gates[key] = gates
            cls._performance[key] = perf
            cls._last_refresh[key] = time.time()

    @classmethod
    def signal_threshold(cls, epic: str, default: float) -> float:
        with cls._lock:
            g = cls._gates.get(str(epic or "").strip())
        return float(g.signal_threshold) if g else float(default)

    @classmethod
    def fitness_min(cls, epic: str, default: float) -> float:
        with cls._lock:
            g = cls._gates.get(str(epic or "").strip())
        return float(g.fitness_min) if g else float(default)

    @classmethod
    def telemetry_snapshot(cls) -> dict[str, Any]:
        with cls._lock:
            return {
                "gates": {k: g.__dict__ for k, g in cls._gates.items()},
                "metrics": {k: m.__dict__ for k, m in cls._metrics.items()},
                "performance": {k: p.__dict__ for k, p in cls._performance.items()},
            }


def effective_gate_pass_min(epic: str) -> float:
    """
    Dynamic environment fitness floor for *epic*.

    Falls back to GATE_PASS_MIN_DEFAULT when adaptation is disabled or unstale.
    """
    if not adaptation_enabled():
        return GATE_PASS_MIN_DEFAULT
    key = str(epic or "").strip()
    with AdaptationRegistry._lock:
        g = AdaptationRegistry._gates.get(key)
        last = AdaptationRegistry._last_refresh.get(key, 0.0)
    if g is not None and (time.time() - last) < (_REFRESH_TTL_SEC * 3):
        return float(g.fitness_min)
    try:
        base_sig = float(_protective_floor())
    except Exception:
        base_sig = 45.0
    refresh_epic_adaptation(key, base_signal=base_sig)
    return AdaptationRegistry.fitness_min(key, GATE_PASS_MIN_DEFAULT)


def refresh_epic_adaptation(
    epic: str,
    *,
    base_signal: float,
    window_sec: float | None = None,
) -> ElasticGates:
    key = str(epic or "").strip()
    cfg = _adaptation_config()
    win = float(window_sec or cfg.get("lookback_sec") or _DEFAULT_LOOKBACK_SEC)
    metrics = compute_lookback_metrics(key, window_sec=win)
    gates = compute_elastic_gates(
        key,
        metrics,
        base_signal=float(base_signal),
        base_fitness=GATE_PASS_MIN_DEFAULT,
    )
    perf = PerformanceFeedbackLoop.load_epic_snapshot(key)
    gates = PerformanceFeedbackLoop.apply_bias(gates, perf)
    AdaptationRegistry.update(key, metrics, gates, perf)

    if gates.regime != "normal" and gates.regime != "sparse":
        log_engine(
            f"[DYNAMIC_ADAPT] epic={key} regime={gates.regime} "
            f"sig={gates.signal_threshold} fit={gates.fitness_min} "
            f"atr_ratio={metrics.atr_ratio:.2f} pf={perf.profit_factor} "
            f"src={perf.source} {gates.detail}"
        )
    return gates


class DynamicAdaptationEngine:
    """Facade for TradingLoop — refresh gates each tick."""

    @staticmethod
    def refresh_for_epic(
        epic: str,
        *,
        base_signal: float,
        window_sec: float | None = None,
    ) -> dict[str, Any]:
        if not adaptation_enabled():
            return {}
        gates = refresh_epic_adaptation(
            epic,
            base_signal=base_signal,
            window_sec=window_sec,
        )
        with AdaptationRegistry._lock:
            metrics = AdaptationRegistry._metrics.get(str(epic or "").strip())
            perf = AdaptationRegistry._performance.get(str(epic or "").strip())
        return {
            "gates": gates,
            "metrics": metrics,
            "performance": perf,
        }

    @staticmethod
    def effective_signal_threshold(epic: str, default: float) -> float:
        if not adaptation_enabled():
            return float(default)
        return AdaptationRegistry.signal_threshold(epic, default)

    @staticmethod
    def effective_fitness_min(epic: str, default: float) -> float:
        if not adaptation_enabled():
            return float(default)
        return AdaptationRegistry.fitness_min(epic, default)


# ---------------------------------------------------------------------------
# Autonomous Fatigue Relaxation — trade-starvation sentinel (v31 sandbox)
# ---------------------------------------------------------------------------

STATE_NORMAL = "NORMAL"
STATE_STARVED = "STATE_STARVED"

_STARVATION_AFTER_SEC = 60 * 60
_DECAY_INTERVAL_SEC = 15 * 60
_DECAY_STEP_PCT = 2.5
_HARD_FLOOR_PCT = 35.0
_SENTINEL_POLL_SEC = 30.0


def _parse_closed_at_ts(raw: Any) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            from datetime import datetime

            dt = datetime.strptime(text.replace("+00:00", "Z"), fmt)
            if dt.tzinfo is None:
                return dt.timestamp()
            return dt.timestamp()
        except ValueError:
            continue
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _agent_healthy_for_starvation() -> bool:
    try:
        from api.agent_health import get_cached_health_status

        health = get_cached_health_status() or {}
        boot = health.get("boot_metrics") or {}
        if boot.get("ready") is True:
            return True
        return bool(health.get("trading_healthy"))
    except Exception:
        return True


def _last_closed_trade_epoch() -> float | None:
    best: float | None = None
    try:
        from data.learning_store import LearningStore

        store = LearningStore()
        if hasattr(store, "recent_confirmed_closed_trades"):
            rows = store.recent_confirmed_closed_trades(limit=5)
            for row in rows or []:
                ts = _parse_closed_at_ts(row.get("closed_at"))
                if ts is not None and (best is None or ts > best):
                    best = ts
    except Exception:
        pass
    ledger = _sandbox_trading_ledger_path()
    if ledger.is_file():
        try:
            payload = json.loads(ledger.read_text(encoding="utf-8"))
            for row in reversed(payload.get("closed") or payload.get("trades") or []):
                if not isinstance(row, dict):
                    continue
                ts = _parse_closed_at_ts(
                    row.get("closed_at") or row.get("exit_time") or row.get("ts")
                )
                if ts is not None and (best is None or ts > best):
                    best = ts
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return best


def _sandbox_trading_ledger_path() -> Path:
    try:
        from system.paths import data_dir

        return data_dir() / "state" / "trading_ledger.json"
    except Exception:
        return Path("src/data/state/trading_ledger.json")


class StarvationSentinel:
    """
    Background trade-starvation tracker — decays ml_veto + alpha floors when idle.

    After 60 minutes with zero trade activity while the agent is healthy, enters
    STATE_STARVED and relaxes floors by 2.5 percentage points every 15 minutes
    (hard stop 35.0%). Any executed trade resets to baseline instantly.
    """

    _lock = threading.RLock()
    _thread: threading.Thread | None = None
    _stop = threading.Event()
    _started = False

    _last_trade_epoch: float | None = None
    _agent_boot_epoch: float = 0.0

    _state: str = STATE_NORMAL
    _starved_since_mono: float | None = None
    _last_decay_mono: float = 0.0
    _decay_steps: int = 0
    _baseline_ml: float = 45.0
    _baseline_alpha: float = 45.0
    _minutes_since_trade: float = 0.0

    _ml_override: float | None = None
    _alpha_override: float | None = None
    _auto_decay_enabled: bool = True

    @classmethod
    def ensure_started(cls) -> None:
        if not (_is_v31_sandbox() and adaptation_enabled()):
            return
        with cls._lock:
            if cls._started:
                return
            cls._agent_boot_epoch = time.time()
            cls._stop.clear()
            cls._thread = threading.Thread(
                target=cls._run_loop,
                name="starvation-sentinel",
                daemon=True,
            )
            cls._thread.start()
            cls._started = True
            log_engine("StarvationSentinel: background tracker armed (60m threshold)")

    @classmethod
    def stop_for_tests(cls) -> None:
        with cls._lock:
            cls._stop.set()
            if cls._thread is not None:
                cls._thread.join(timeout=2.0)
            cls._thread = None
            cls._started = False
            cls._reset_unlocked()

    @classmethod
    def record_trade_execution(cls, *, source: str = "execution") -> None:
        """Instant reset when any trade fires (submit / fill / close)."""
        if not (_is_v31_sandbox() and adaptation_enabled()):
            return
        now = time.time()
        with cls._lock:
            cls._last_trade_epoch = now
            if cls._state == STATE_STARVED or cls._decay_steps > 0:
                log_engine(
                    f"StarvationSentinel: trade activity ({source}) — "
                    "floors reset to baseline"
                )
            cls._reset_unlocked()

    @classmethod
    def apply_tune(
        cls,
        *,
        ml_veto_override: float | None = None,
        alpha_seed_override: float | None = None,
        auto_decay_enabled: bool | None = None,
        clear_overrides: bool = False,
    ) -> dict[str, Any]:
        with cls._lock:
            if clear_overrides:
                cls._ml_override = None
                cls._alpha_override = None
            if ml_veto_override is not None:
                cls._ml_override = max(
                    _HARD_FLOOR_PCT, min(99.0, float(ml_veto_override))
                )
            if alpha_seed_override is not None:
                cls._alpha_override = max(
                    _HARD_FLOOR_PCT, min(99.0, float(alpha_seed_override))
                )
            if auto_decay_enabled is not None:
                cls._auto_decay_enabled = bool(auto_decay_enabled)
        return cls.status_payload()

    @classmethod
    def capture_baseline_floors(
        cls,
        signal_floor: float,
        fitness_floor: float,
        ml_floor: float,
    ) -> None:
        """Track matrix baselines while not in decay."""
        with cls._lock:
            if cls._state != STATE_STARVED and cls._decay_steps == 0:
                alpha = max(float(signal_floor), float(fitness_floor))
                if alpha > 0:
                    cls._baseline_alpha = alpha
                if float(ml_floor) > 0:
                    cls._baseline_ml = float(ml_floor)

    @classmethod
    def apply_floor_overrides(
        cls,
        signal_floor: float,
        fitness_floor: float,
        ml_floor: float,
    ) -> tuple[float, float, float]:
        cls.ensure_started()
        sig = float(signal_floor)
        fit = float(fitness_floor)
        ml = float(ml_floor)

        with cls._lock:
            alpha_base = cls._baseline_alpha if cls._baseline_alpha > 0 else max(sig, fit)
            ml_base = cls._baseline_ml if cls._baseline_ml > 0 else ml

            if cls._alpha_override is not None:
                sig = fit = float(cls._alpha_override)
            elif cls._state == STATE_STARVED and cls._auto_decay_enabled:
                decayed = max(
                    _HARD_FLOOR_PCT,
                    alpha_base - (cls._decay_steps * _DECAY_STEP_PCT),
                )
                sig = fit = decayed

            if cls._ml_override is not None:
                ml = float(cls._ml_override)
            elif cls._state == STATE_STARVED and cls._auto_decay_enabled:
                ml = max(
                    _HARD_FLOOR_PCT,
                    ml_base - (cls._decay_steps * _DECAY_STEP_PCT),
                )

        return round(sig, 2), round(fit, 2), round(ml, 4)

    @classmethod
    def status_payload(cls) -> dict[str, Any]:
        with cls._lock:
            starvation_risk = cls._minutes_since_trade >= (_STARVATION_AFTER_SEC / 60.0 - 1.0)
            return {
                "state": cls._state,
                "starvation_risk": starvation_risk,
                "minutes_since_trade": round(cls._minutes_since_trade, 1),
                "minutes_to_starvation": round(
                    max(0.0, (_STARVATION_AFTER_SEC / 60.0) - cls._minutes_since_trade),
                    1,
                ),
                "decay_steps": cls._decay_steps,
                "auto_decay_enabled": cls._auto_decay_enabled,
                "ml_veto_override": cls._ml_override,
                "alpha_seed_override": cls._alpha_override,
                "baseline_ml_floor": round(cls._baseline_ml, 2),
                "baseline_alpha_floor": round(cls._baseline_alpha, 2),
                "effective_ml_floor": round(
                    cls._ml_override
                    if cls._ml_override is not None
                    else max(
                        _HARD_FLOOR_PCT,
                        cls._baseline_ml - cls._decay_steps * _DECAY_STEP_PCT,
                    ),
                    2,
                ),
                "effective_alpha_floor": round(
                    cls._alpha_override
                    if cls._alpha_override is not None
                    else max(
                        _HARD_FLOOR_PCT,
                        cls._baseline_alpha - cls._decay_steps * _DECAY_STEP_PCT,
                    ),
                    2,
                ),
                "hard_floor_pct": _HARD_FLOOR_PCT,
                "last_trade_epoch": cls._last_trade_epoch,
            }

    @classmethod
    def _reset_unlocked(cls) -> None:
        cls._state = STATE_NORMAL
        cls._starved_since_mono = None
        cls._last_decay_mono = 0.0
        cls._decay_steps = 0

    @classmethod
    def _resolve_minutes_since_trade(cls) -> float:
        with cls._lock:
            manual = cls._last_trade_epoch
        closed = _last_closed_trade_epoch()
        candidates = [t for t in (manual, closed, cls._agent_boot_epoch) if t]
        if not candidates:
            return 0.0
        last = max(candidates)
        return max(0.0, (time.time() - last) / 60.0)

    @classmethod
    def _tick(cls) -> None:
        if not (_is_v31_sandbox() and adaptation_enabled()):
            return
        minutes = cls._resolve_minutes_since_trade()
        healthy = _agent_healthy_for_starvation()
        now_mono = time.monotonic()

        with cls._lock:
            cls._minutes_since_trade = minutes
            if not healthy:
                return

            if minutes < (_STARVATION_AFTER_SEC / 60.0):
                if cls._state == STATE_STARVED:
                    cls._reset_unlocked()
                return

            if cls._state != STATE_STARVED:
                cls._state = STATE_STARVED
                cls._starved_since_mono = now_mono
                cls._last_decay_mono = now_mono
                log_engine(
                    f"[STARVATION_SENTINEL] {STATE_STARVED} "
                    f"minutes_since_trade={minutes:.0f} — autonomous decay armed"
                )

            if not cls._auto_decay_enabled:
                return

            while (
                cls._starved_since_mono is not None
                and (now_mono - cls._last_decay_mono) >= _DECAY_INTERVAL_SEC
            ):
                prospective = cls._baseline_alpha - ((cls._decay_steps + 1) * _DECAY_STEP_PCT)
                if prospective < _HARD_FLOOR_PCT:
                    break
                cls._decay_steps += 1
                cls._last_decay_mono += _DECAY_INTERVAL_SEC
                eff_alpha = max(
                    _HARD_FLOOR_PCT,
                    cls._baseline_alpha - cls._decay_steps * _DECAY_STEP_PCT,
                )
                eff_ml = max(
                    _HARD_FLOOR_PCT,
                    cls._baseline_ml - cls._decay_steps * _DECAY_STEP_PCT,
                )
                log_engine(
                    f"[STARVATION_SENTINEL] decay_step={cls._decay_steps} "
                    f"alpha_floor={eff_alpha:.1f}% ml_veto={eff_ml:.1f}% "
                    f"(hard_stop={_HARD_FLOOR_PCT:.1f}%)"
                )

    @classmethod
    def _run_loop(cls) -> None:
        while not cls._stop.is_set():
            try:
                cls._tick()
            except Exception as exc:
                log_engine(
                    f"StarvationSentinel loop error: {type(exc).__name__}: {exc}"
                )
            if cls._stop.wait(_SENTINEL_POLL_SEC):
                break

