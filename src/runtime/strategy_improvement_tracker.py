"""
Strategy improvement measurement — rolling WR/PnL by regime and strategy epoch.

Records every managed exit and compares performance across strategy shifts
(regime changes, parameter_tuner overlay updates, ML model retrains).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_lock = threading.RLock()
_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "strategy_improvement.json"
_WIN_RATE_TARGET = 0.70


@dataclass
class TradeCloseRecord:
    ts: float
    epic: str
    pnl_gbp: float
    exit_reason: str
    regime_state: int | None = None
    strategy_epoch: str = ""
    ml_label_count: int = 0
    won: bool = False
    profit_tier_pct: float | None = None
    peak_pct_of_target: float | None = None
    profit_pct_of_target: float | None = None
    hold_sec: float | None = None
    sentiment_delta_5m: float | None = None
    news_countdown_norm: float | None = None
    runner_extended: bool = False


@dataclass
class WindowStats:
    n: int = 0
    wins: int = 0
    win_rate: float = 0.0
    total_pnl_gbp: float = 0.0
    avg_pnl_gbp: float = 0.0


@dataclass
class StrategyImprovementState:
    strategy_epoch: str = "init"
    epoch_started_at: float = 0.0
    last_overlay_hash: str = ""
    last_model_train_ts: float = 0.0
    closes: list[dict[str, Any]] = field(default_factory=list)
    by_exit_reason: dict[str, WindowStats] = field(default_factory=dict)


_state = StrategyImprovementState(epoch_started_at=time.time())


def _window_stats(records: list[dict[str, Any]]) -> WindowStats:
    if not records:
        return WindowStats()
    n = len(records)
    wins = sum(1 for r in records if r.get("won"))
    total = sum(float(r.get("pnl_gbp") or 0) for r in records)
    return WindowStats(
        n=n,
        wins=wins,
        win_rate=round(wins / n, 4) if n else 0.0,
        total_pnl_gbp=round(total, 2),
        avg_pnl_gbp=round(total / n, 2) if n else 0.0,
    )


def _overlay_fingerprint() -> str:
    try:
        from runtime.parameter_tuner import get_regime_matrix

        matrix = get_regime_matrix()
        return str(hash(json.dumps(matrix, sort_keys=True)))
    except Exception:
        return ""


def _current_regime_state() -> int | None:
    try:
        from runtime.regime_switch_engine import get_regime_switch_snapshot

        snap = get_regime_switch_snapshot()
        markets = snap.get("markets") or []
        if markets:
            return int(markets[0].get("regime_state", markets[0].get("state", 0)) or 0)
    except Exception:
        pass
    return None


def _ml_label_count() -> int:
    try:
        from data.ml_training_store import MLTrainingStore
        from system.paths import data_dir

        store = MLTrainingStore(str(data_dir() / "ml_training_store.jsonl"))
        return int(store.record_count())
    except Exception:
        return 0


def _maybe_rotate_epoch(*, reason: str) -> None:
    global _state
    fp = _overlay_fingerprint()
    if fp and fp != _state.last_overlay_hash and _state.last_overlay_hash:
        _state.strategy_epoch = f"overlay_{int(time.time())}"
        _state.epoch_started_at = time.time()
        log_engine(
            f"strategy_improvement: new epoch ({reason}) — {_state.strategy_epoch}"
        )
    if fp:
        _state.last_overlay_hash = fp


def record_managed_close(
    *,
    epic: str,
    pnl_gbp: float,
    exit_reason: str,
    regime_state: int | None = None,
    profit_tier_pct: float | None = None,
    peak_pct_of_target: float | None = None,
    profit_pct_of_target: float | None = None,
    hold_sec: float | None = None,
    sentiment_delta_5m: float | None = None,
    news_countdown_norm: float | None = None,
    runner_extended: bool = False,
) -> str | None:
    """Record a position close from OpenPositionManager / micro_gbp_exit. Returns session_slot."""
    global _state
    won = float(pnl_gbp) > 0.05
    if profit_tier_pct is None:
        try:
            from execution.profit_pct_tiers import classify_tier_pct_from_reason

            profit_tier_pct = classify_tier_pct_from_reason(exit_reason)
        except Exception:
            pass
    rec = TradeCloseRecord(
        ts=time.time(),
        epic=str(epic or ""),
        pnl_gbp=round(float(pnl_gbp), 2),
        exit_reason=str(exit_reason or "unknown")[:120],
        regime_state=regime_state if regime_state is not None else _current_regime_state(),
        strategy_epoch=_state.strategy_epoch,
        ml_label_count=_ml_label_count(),
        won=won,
        profit_tier_pct=profit_tier_pct,
        peak_pct_of_target=peak_pct_of_target,
        profit_pct_of_target=profit_pct_of_target,
        hold_sec=hold_sec,
        sentiment_delta_5m=sentiment_delta_5m,
        news_countdown_norm=news_countdown_norm,
        runner_extended=bool(runner_extended),
    )
    session_slot: str | None = None
    try:
        from runtime.intraday_slot_tracker import record_slot_close
        from system.config_loader import get_config

        session_slot = record_slot_close(
            epic=rec.epic,
            pnl_gbp=rec.pnl_gbp,
            exit_reason=rec.exit_reason,
            ts=rec.ts,
            strategy_epoch=rec.strategy_epoch,
            won=rec.won,
            cfg=get_config(),
        )
    except Exception:
        pass

    with _lock:
        _maybe_rotate_epoch(reason="tuner_overlay")
        close_dict = asdict(rec)
        if session_slot:
            close_dict["session_slot"] = session_slot
        _state.closes.append(close_dict)
        if len(_state.closes) > 500:
            _state.closes = _state.closes[-500:]
        _persist_unlocked()
    return session_slot


def note_ml_model_trained() -> None:
    """Call after auto_trainer completes — starts new measurement epoch."""
    global _state
    with _lock:
        _state.last_model_train_ts = time.time()
        _state.strategy_epoch = f"ml_{int(time.time())}"
        _state.epoch_started_at = time.time()
        log_engine(f"strategy_improvement: ML retrain epoch {_state.strategy_epoch}")
        _persist_unlocked()


def list_managed_closes(*, limit: int = 200) -> list[dict[str, Any]]:
    """Return persisted managed-close records for API / ML assessment."""
    _ensure_persisted_loaded()
    cap = max(1, min(int(limit), 500))
    with _lock:
        return list(_state.closes[-cap:])


def snapshot(*, window: int = 20) -> dict[str, Any]:
    """Rolling improvement metrics for API / Trading Desk."""
    with _lock:
        closes = list(_state.closes)
        epoch = _state.strategy_epoch
        epoch_started = _state.epoch_started_at

    w10 = _window_stats(closes[-10:])
    w20 = _window_stats(closes[-window:])
    w50 = _window_stats(closes[-50:])
    epoch_closes = [c for c in closes if c.get("strategy_epoch") == epoch]
    epoch_stats = _window_stats(epoch_closes)

    by_reason: dict[str, dict[str, Any]] = {}
    for c in closes[-50:]:
        reason = str(c.get("exit_reason") or "unknown").split()[0]
        bucket = by_reason.setdefault(reason, {"records": []})
        bucket["records"].append(c)
    for reason, bucket in by_reason.items():
        bucket["stats"] = asdict(_window_stats(bucket["records"]))
        del bucket["records"]

    improving = (
        w20.n >= 6
        and w20.win_rate >= _WIN_RATE_TARGET
        and w20.total_pnl_gbp > 0
    )
    delta_wr = round(w20.win_rate - w10.win_rate, 4) if w10.n >= 5 and w20.n >= 10 else None

    profit_tiers: dict[str, Any] = {}
    try:
        from execution.profit_pct_tiers import assess_profit_tier_strategy
        from system.config_loader import get_config

        profit_tiers = assess_profit_tier_strategy(closes[-100:], cfg=get_config())
    except Exception:
        pass

    intraday_slots: dict[str, Any] = {}
    try:
        from runtime.intraday_slot_tracker import snapshot as intraday_snapshot
        from system.config_loader import get_config

        intraday_slots = intraday_snapshot(cfg=get_config())
    except Exception:
        pass

    return {
        "ok": True,
        "strategy_epoch": epoch,
        "epoch_age_sec": round(max(0.0, time.time() - epoch_started), 1),
        "win_rate_target": _WIN_RATE_TARGET,
        "improving": improving,
        "delta_wr_10v20": delta_wr,
        "ml_label_count": _ml_label_count(),
        "windows": {
            "last_10": asdict(w10),
            "last_20": asdict(w20),
            "last_50": asdict(w50),
            "current_epoch": asdict(epoch_stats),
        },
        "by_exit_reason": by_reason,
        "profit_tier_assessment": profit_tiers,
        "intraday_slots": intraday_slots,
        "recent_closes": closes[-8:],
    }


def _persist_unlocked() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy_epoch": _state.strategy_epoch,
            "epoch_started_at": _state.epoch_started_at,
            "last_overlay_hash": _state.last_overlay_hash,
            "last_model_train_ts": _state.last_model_train_ts,
            "closes": _state.closes[-200:],
        }
        _STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_persisted_state() -> None:
    global _state
    if not _STATE_PATH.exists():
        return
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        with _lock:
            _state.strategy_epoch = str(raw.get("strategy_epoch") or "init")
            _state.epoch_started_at = float(raw.get("epoch_started_at") or time.time())
            _state.last_overlay_hash = str(raw.get("last_overlay_hash") or "")
            _state.last_model_train_ts = float(raw.get("last_model_train_ts") or 0)
            closes = raw.get("closes")
            _state.closes = list(closes) if isinstance(closes, list) else []
    except Exception:
        pass


def _ensure_persisted_loaded() -> None:
    with _lock:
        if _state.closes:
            return
    if _STATE_PATH.exists():
        load_persisted_state()


def reset_strategy_improvement_for_tests() -> None:
    global _state
    with _lock:
        _state = StrategyImprovementState(epoch_started_at=time.time())
