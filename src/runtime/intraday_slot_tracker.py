"""
Intraday time-slot performance tracking — WR/PnL by BST session window.

Buckets managed closes into configurable slots (pre-Europe, US cash, overnight, …)
and scores improvement vs the prior epoch/day for the same slot.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from system.engine_log import log_engine

_lock = threading.RLock()
_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "intraday_slot_performance.json"
_MAX_CLOSES_PER_SLOT = 50
_DEFAULT_TZ = "Europe/London"
_DEFAULT_TARGET_MIN_DELTA_WR = 0.01

_DEFAULT_SLOTS: list[dict[str, str]] = [
    {"id": "pre_europe", "label": "Pre-Europe", "start": "06:00", "end": "08:00"},
    {"id": "europe_open", "label": "Europe Open", "start": "08:00", "end": "09:30"},
    {"id": "us_premarket", "label": "US Pre-Market", "start": "09:30", "end": "14:30"},
    {"id": "us_cash", "label": "US Cash", "start": "14:30", "end": "17:00"},
    {"id": "us_close", "label": "US Close", "start": "17:00", "end": "21:00"},
    {"id": "overnight", "label": "Overnight", "start": "21:00", "end": "06:00"},
]


@dataclass
class SlotStats:
    n: int = 0
    wins: int = 0
    win_rate: float = 0.0
    total_pnl_gbp: float = 0.0
    avg_pnl_gbp: float = 0.0
    closes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IntradaySlotState:
    trading_day: str = ""
    strategy_epoch: str = "init"
    slots: dict[str, SlotStats] = field(default_factory=dict)
    prior_day_slots: dict[str, SlotStats] = field(default_factory=dict)
    prior_epoch_slots: dict[str, SlotStats] = field(default_factory=dict)


_state = IntradaySlotState()


def _parse_hhmm(text: str) -> dt_time:
    parts = str(text or "00:00").strip().split(":")
    hour = int(parts[0]) if parts else 0
    minute = int(parts[1]) if len(parts) > 1 else 0
    return dt_time(hour=hour, minute=minute)


def _slots_cfg(cfg: Any | None) -> list[dict[str, str]]:
    block = _cfg_block(cfg)
    raw = block.get("slots")
    if isinstance(raw, list) and raw:
        out: list[dict[str, str]] = []
        for row in raw:
            if isinstance(row, dict) and row.get("id"):
                out.append(
                    {
                        "id": str(row["id"]),
                        "label": str(row.get("label") or row["id"]),
                        "start": str(row.get("start") or "00:00"),
                        "end": str(row.get("end") or "00:00"),
                    }
                )
        if out:
            return out
    return list(_DEFAULT_SLOTS)


def _cfg_block(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    raw = getattr(cfg, "intraday_slots", None)
    if raw is None and hasattr(cfg, "get"):
        raw = cfg.get("intraday_slots")
    return dict(raw) if isinstance(raw, dict) else {}


def intraday_slots_enabled(cfg: Any | None) -> bool:
    return bool(_cfg_block(cfg).get("enabled", False))


def _timezone(cfg: Any | None) -> ZoneInfo:
    tz_name = str(_cfg_block(cfg).get("timezone") or _DEFAULT_TZ)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


def _target_min_delta_wr(cfg: Any | None) -> float:
    try:
        return float(_cfg_block(cfg).get("target_min_delta_wr", _DEFAULT_TARGET_MIN_DELTA_WR))
    except (TypeError, ValueError):
        return _DEFAULT_TARGET_MIN_DELTA_WR


def _trading_day_for_ts(ts: float, cfg: Any | None) -> str:
    tz = _timezone(cfg)
    return datetime.fromtimestamp(ts, tz=tz).date().isoformat()


def slot_id_for_timestamp(ts: float, cfg: Any | None) -> str:
    """Pure helper — map unix timestamp to configured intraday slot id."""
    tz = _timezone(cfg)
    dt = datetime.fromtimestamp(ts, tz=tz)
    t = dt.time()
    for slot in _slots_cfg(cfg):
        start = _parse_hhmm(slot["start"])
        end = _parse_hhmm(slot["end"])
        if start <= end:
            if start <= t < end:
                return str(slot["id"])
        elif t >= start or t < end:
            return str(slot["id"])
    return "unknown"


def _stats_from_closes(closes: list[dict[str, Any]]) -> SlotStats:
    if not closes:
        return SlotStats()
    n = len(closes)
    wins = sum(1 for c in closes if c.get("won"))
    total = sum(float(c.get("pnl_gbp") or 0) for c in closes)
    return SlotStats(
        n=n,
        wins=wins,
        win_rate=round(wins / n, 4) if n else 0.0,
        total_pnl_gbp=round(total, 2),
        avg_pnl_gbp=round(total / n, 2) if n else 0.0,
        closes=list(closes[-_MAX_CLOSES_PER_SLOT:]),
    )


def _improvement_flags(
    current: SlotStats,
    prior: SlotStats | None,
    *,
    target_min_delta_wr: float,
) -> dict[str, Any]:
    if prior is None or prior.n == 0:
        return {
            "delta_wr": None,
            "delta_pnl_gbp": None,
            "improving": None,
            "regressing": None,
            "prior_n": prior.n if prior else 0,
        }
    delta_wr = round(current.win_rate - prior.win_rate, 4)
    delta_pnl = round(current.total_pnl_gbp - prior.total_pnl_gbp, 2)
    improving = delta_wr >= target_min_delta_wr or delta_pnl > 0
    regressing = delta_wr < -target_min_delta_wr
    return {
        "delta_wr": delta_wr,
        "delta_pnl_gbp": delta_pnl,
        "improving": improving,
        "regressing": regressing,
        "prior_n": prior.n,
        "prior_win_rate": prior.win_rate,
        "prior_total_pnl_gbp": prior.total_pnl_gbp,
    }


def _ensure_slot_bucket(slot_id: str) -> SlotStats:
    if slot_id not in _state.slots:
        _state.slots[slot_id] = SlotStats()
    return _state.slots[slot_id]


def _maybe_roll_trading_day(ts: float, cfg: Any | None) -> None:
    day = _trading_day_for_ts(ts, cfg)
    if not _state.trading_day:
        _state.trading_day = day
        return
    if day == _state.trading_day:
        return
    for slot_id, stats in _state.slots.items():
        if stats.n > 0:
            _state.prior_day_slots[slot_id] = SlotStats(
                n=stats.n,
                wins=stats.wins,
                win_rate=stats.win_rate,
                total_pnl_gbp=stats.total_pnl_gbp,
                avg_pnl_gbp=stats.avg_pnl_gbp,
                closes=[],
            )
    _state.trading_day = day
    _state.slots = {}
    log_engine(f"intraday_slots: rolled trading day → {day}")


def _maybe_roll_strategy_epoch(epoch: str) -> None:
    epoch = str(epoch or "init")
    if not _state.strategy_epoch or _state.strategy_epoch == epoch:
        _state.strategy_epoch = epoch
        return
    for slot_id, stats in _state.slots.items():
        if stats.n > 0:
            _state.prior_epoch_slots[slot_id] = SlotStats(
                n=stats.n,
                wins=stats.wins,
                win_rate=stats.win_rate,
                total_pnl_gbp=stats.total_pnl_gbp,
                avg_pnl_gbp=stats.avg_pnl_gbp,
                closes=[],
            )
    _state.strategy_epoch = epoch
    _state.slots = {}
    log_engine(f"intraday_slots: new strategy epoch → {epoch}")


def record_slot_close(
    *,
    epic: str,
    pnl_gbp: float,
    exit_reason: str,
    ts: float | None = None,
    strategy_epoch: str = "",
    won: bool | None = None,
    cfg: Any | None = None,
) -> str | None:
    """Bucket a managed close into the current intraday slot. Returns slot id."""
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
    if not intraday_slots_enabled(cfg):
        return None

    close_ts = float(ts if ts is not None else time.time())
    slot_id = slot_id_for_timestamp(close_ts, cfg)
    is_win = bool(won) if won is not None else float(pnl_gbp) > 0.05
    close_rec = {
        "ts": close_ts,
        "epic": str(epic or ""),
        "pnl_gbp": round(float(pnl_gbp), 2),
        "exit_reason": str(exit_reason or "unknown")[:120],
        "won": is_win,
        "strategy_epoch": str(strategy_epoch or _state.strategy_epoch or "init"),
        "slot_id": slot_id,
    }

    with _lock:
        _maybe_roll_trading_day(close_ts, cfg)
        if strategy_epoch:
            _maybe_roll_strategy_epoch(strategy_epoch)
        bucket = _ensure_slot_bucket(slot_id)
        bucket.closes.append(close_rec)
        if len(bucket.closes) > _MAX_CLOSES_PER_SLOT:
            bucket.closes = bucket.closes[-_MAX_CLOSES_PER_SLOT:]
        refreshed = _stats_from_closes(bucket.closes)
        bucket.n = refreshed.n
        bucket.wins = refreshed.wins
        bucket.win_rate = refreshed.win_rate
        bucket.total_pnl_gbp = refreshed.total_pnl_gbp
        bucket.avg_pnl_gbp = refreshed.avg_pnl_gbp
        _persist_unlocked()
    return slot_id


def snapshot(*, cfg: Any | None = None) -> dict[str, Any]:
    """API snapshot — all slots, current highlight, improvement flags, day totals."""
    _ensure_persisted_loaded()
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None

    enabled = intraday_slots_enabled(cfg)
    now = time.time()
    current_slot = slot_id_for_timestamp(now, cfg) if enabled else None
    target_delta = _target_min_delta_wr(cfg)
    slot_defs = _slots_cfg(cfg)

    with _lock:
        trading_day = _state.trading_day or _trading_day_for_ts(now, cfg)
        epoch = _state.strategy_epoch
        slots_state = {k: SlotStats(**asdict(v)) for k, v in _state.slots.items()}
        prior_day = {k: SlotStats(**asdict(v)) for k, v in _state.prior_day_slots.items()}
        prior_epoch = {k: SlotStats(**asdict(v)) for k, v in _state.prior_epoch_slots.items()}

    slots_out: dict[str, Any] = {}
    all_closes: list[dict[str, Any]] = []
    for slot_def in slot_defs:
        sid = slot_def["id"]
        current = slots_state.get(sid) or SlotStats()
        all_closes.extend(current.closes)
        prior = prior_epoch.get(sid)
        if prior is None or prior.n == 0:
            prior = prior_day.get(sid)
        imp = _improvement_flags(current, prior, target_min_delta_wr=target_delta)
        slots_out[sid] = {
            "id": sid,
            "label": slot_def["label"],
            "start": slot_def["start"],
            "end": slot_def["end"],
            "current": asdict(current),
            "improvement": imp,
            "is_current": sid == current_slot,
        }

    day_stats = _stats_from_closes(all_closes)
    improving_slots = sum(
        1 for s in slots_out.values() if s.get("improvement", {}).get("improving") is True
    )
    regressing_slots = sum(
        1 for s in slots_out.values() if s.get("improvement", {}).get("regressing") is True
    )

    return {
        "ok": True,
        "enabled": enabled,
        "timezone": str(_cfg_block(cfg).get("timezone") or _DEFAULT_TZ),
        "trading_day": trading_day,
        "strategy_epoch": epoch,
        "current_slot_id": current_slot,
        "target_min_delta_wr": target_delta,
        "day_totals": asdict(day_stats),
        "improving_slots": improving_slots,
        "regressing_slots": regressing_slots,
        "slots": slots_out,
    }


def _persist_unlocked() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "trading_day": _state.trading_day,
            "strategy_epoch": _state.strategy_epoch,
            "slots": {
                sid: {
                    **asdict(stats),
                    "closes": stats.closes[-_MAX_CLOSES_PER_SLOT:],
                }
                for sid, stats in _state.slots.items()
            },
            "prior_day_slots": {sid: asdict(s) for sid, s in _state.prior_day_slots.items()},
            "prior_epoch_slots": {sid: asdict(s) for sid, s in _state.prior_epoch_slots.items()},
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
            _state.trading_day = str(raw.get("trading_day") or "")
            _state.strategy_epoch = str(raw.get("strategy_epoch") or "init")
            _state.slots = {}
            for sid, row in (raw.get("slots") or {}).items():
                if isinstance(row, dict):
                    closes = row.get("closes")
                    _state.slots[str(sid)] = SlotStats(
                        n=int(row.get("n") or 0),
                        wins=int(row.get("wins") or 0),
                        win_rate=float(row.get("win_rate") or 0),
                        total_pnl_gbp=float(row.get("total_pnl_gbp") or 0),
                        avg_pnl_gbp=float(row.get("avg_pnl_gbp") or 0),
                        closes=list(closes) if isinstance(closes, list) else [],
                    )
            _state.prior_day_slots = _load_stats_map(raw.get("prior_day_slots"))
            _state.prior_epoch_slots = _load_stats_map(raw.get("prior_epoch_slots"))
    except Exception:
        pass


def _ensure_persisted_loaded() -> None:
    with _lock:
        if _state.trading_day or _state.slots:
            return
    if _STATE_PATH.exists():
        load_persisted_state()


def _load_stats_map(raw: Any) -> dict[str, SlotStats]:
    out: dict[str, SlotStats] = {}
    if not isinstance(raw, dict):
        return out
    for sid, row in raw.items():
        if isinstance(row, dict):
            out[str(sid)] = SlotStats(
                n=int(row.get("n") or 0),
                wins=int(row.get("wins") or 0),
                win_rate=float(row.get("win_rate") or 0),
                total_pnl_gbp=float(row.get("total_pnl_gbp") or 0),
                avg_pnl_gbp=float(row.get("avg_pnl_gbp") or 0),
                closes=[],
            )
    return out


def reset_intraday_slot_tracker_for_tests() -> None:
    global _state
    with _lock:
        _state = IntradaySlotState()
    try:
        if _STATE_PATH.exists():
            _STATE_PATH.unlink()
    except Exception:
        pass
