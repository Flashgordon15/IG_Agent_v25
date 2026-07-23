"""Session win-rate and profit-target gates — 70% WR floor, £1k daily on £10k capital."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

WIN_RATE_FLOOR = 0.70
DEFAULT_MIN_LABELED = 6
DEFAULT_TARGET_GBP = 1000.0
DEFAULT_CAPITAL_GBP = 10000.0
_LONDON = ZoneInfo("Europe/London")


def _resolve_cfg(cfg: Any | None) -> Any:
    """Prefer explicit cfg; otherwise load (auto-reloads on disk mtime)."""
    if cfg is not None:
        return cfg
    try:
        from system.config_loader import get_config

        return get_config()
    except Exception:
        return None


def _quality_block(cfg: Any) -> dict[str, Any]:
    resolved = _resolve_cfg(cfg)
    raw = resolved.get("strategy_quality") if resolved is not None else None
    if isinstance(raw, dict):
        return raw
    return {}


def _demo_throughput_enabled(cfg: Any | None = None) -> bool:
    """Demo soak: WR / session-PnL emergency entry halts must not freeze the desk."""
    resolved = _resolve_cfg(cfg)
    if resolved is None:
        return False
    block = resolved.get("demo_throughput_mode")
    if isinstance(block, dict):
        return bool(block.get("enabled", False))
    return False


def strategy_quality_enabled(cfg: Any | None = None) -> bool:
    return bool(_quality_block(cfg).get("enabled", False))


def _is_fail_safe_null_close(record: dict[str, Any]) -> bool:
    """£0 / null-UPL fail-safe closes are not real strategy losses."""
    reason = str(
        record.get("reason")
        or record.get("exit_reason")
        or record.get("close_reason")
        or ""
    ).lower()
    if "null_fail_safe" in reason or "upl_null" in reason:
        return True
    if "fail_safe" in reason and abs(float(record.get("pnl_gbp") or 0)) < 0.05:
        return True
    return False


def _close_is_win(record: dict[str, Any]) -> bool:
    if _is_fail_safe_null_close(record):
        return False
    if record.get("won"):
        return True
    pnl = float(record.get("pnl_gbp") or 0)
    return pnl > 0.05


def _close_is_loss(record: dict[str, Any]) -> bool:
    if _is_fail_safe_null_close(record):
        return False
    if record.get("won") is False:
        pnl = float(record.get("pnl_gbp") or 0)
        return pnl < -0.05
    pnl = float(record.get("pnl_gbp") or 0)
    return pnl < -0.05


def session_labeled_win_rate(
    *, session_day: str | None = None, cfg: Any | None = None
) -> tuple[int, int, int, float]:
    """Return (wins, losses, total_labeled, win_rate) for agent closes on session_day."""
    day = session_day or date.today().isoformat()
    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config
        from system.learning_trade_policy import agent_trades_sql_clause

        db_path = None
        if cfg is not None:
            db_path = cfg.get("learning_db") if isinstance(cfg, dict) else getattr(cfg, "learning_db", None)
        if not db_path:
            db_path = get_config().learning_db
        store = LearningStore(str(db_path))
        clause = agent_trades_sql_clause()
        rows = store.conn.execute(
            f"""
            SELECT result FROM trades
            WHERE closed_at IS NOT NULL
              AND closed_at LIKE ?
              AND result IN ('WIN', 'LOSS')
              AND {clause}
            """,
            (f"{day}%",),
        ).fetchall()
        wins = sum(1 for r in rows if str(r["result"]) == "WIN")
        losses = sum(1 for r in rows if str(r["result"]) == "LOSS")
        total = wins + losses
        wr = (wins / total) if total else 0.0
        return wins, losses, total, float(wr)
    except Exception:
        return 0, 0, 0, 0.0


def session_managed_win_rate(
    *, session_day: str | None = None, cfg: Any | None = None
) -> tuple[int, int, int, float]:
    """Win rate from managed closes (strategy_improvement_tracker) — authoritative for desk exits."""
    day = session_day or date.today().isoformat()
    try:
        from runtime.strategy_improvement_tracker import list_managed_closes

        closes = list_managed_closes(limit=500)
        wins = losses = 0
        for record in closes:
            ts = float(record.get("ts") or 0)
            if ts <= 0:
                continue
            close_day = datetime.fromtimestamp(ts, tz=_LONDON).date().isoformat()
            if close_day != day:
                continue
            if _close_is_win(record):
                wins += 1
            elif _close_is_loss(record):
                losses += 1
        total = wins + losses
        wr = (wins / total) if total else 0.0
        return wins, losses, total, float(wr)
    except Exception:
        return 0, 0, 0, 0.0


def rolling_managed_win_rate(
    *, window: int = 20, cfg: Any | None = None
) -> tuple[int, int, int, float]:
    """Rolling WR over the last N managed closes (cross-day — fast churn detector)."""
    try:
        from runtime.strategy_improvement_tracker import list_managed_closes

        cap = max(1, int(window))
        closes = list_managed_closes(limit=cap)[-cap:]
        wins = sum(1 for r in closes if _close_is_win(r))
        losses = sum(1 for r in closes if _close_is_loss(r))
        total = wins + losses
        wr = (wins / total) if total else 0.0
        return wins, losses, total, float(wr)
    except Exception:
        return 0, 0, 0, 0.0


def consecutive_managed_loss_streak(*, cfg: Any | None = None) -> int:
    """Count consecutive losing managed closes (most recent first).

    Skips broker_upl_null_fail_safe / £0 fail-safe closes so a feed outage
    cannot permanently pause the desk via loss_streak_pause.
    """
    try:
        from runtime.strategy_improvement_tracker import list_managed_closes

        streak = 0
        for record in reversed(list_managed_closes(limit=30)):
            if _is_fail_safe_null_close(record):
                continue
            if _close_is_loss(record):
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def session_managed_pnl_gbp(*, session_day: str | None = None, cfg: Any | None = None) -> float:
    """Sum managed-close PnL for session_day (London)."""
    day = session_day or date.today().isoformat()
    try:
        from runtime.strategy_improvement_tracker import list_managed_closes

        total = 0.0
        for record in list_managed_closes(limit=500):
            ts = float(record.get("ts") or 0)
            if ts <= 0:
                continue
            close_day = datetime.fromtimestamp(ts, tz=_LONDON).date().isoformat()
            if close_day != day:
                continue
            total += float(record.get("pnl_gbp") or 0)
        return float(total)
    except Exception:
        return 0.0


def evaluate_desk_halt_gate(cfg: Any | None = None) -> tuple[bool, str, dict[str, Any]]:
    """Emergency / operator desk entry halt — manual flag or auto WR/PnL breach."""
    block = _quality_block(cfg)
    demo_soak = _demo_throughput_enabled(cfg)
    value: dict[str, Any] = {
        "desk_halt_entries": bool(block.get("desk_halt_entries", False)),
        "demo_throughput_soak": demo_soak,
    }

    if bool(block.get("desk_halt_entries", False)):
        return (
            False,
            "desk_halt_entries flag active — clear strategy_quality.desk_halt_entries to resume",
            value,
        )

    # Default 0.0 in demo soak so a stale in-memory config missing the key
    # cannot re-arm the 20% WR freeze / session-PnL freeze. Non-demo keeps
    # the safety defaults (20% WR, -£200 session PnL).
    default_wr = 0.0 if demo_soak else 0.20
    default_pnl = 0.0 if demo_soak else -200.0
    raw_wr = block.get("emergency_rolling_wr_floor", default_wr)
    emergency_wr = default_wr if raw_wr is None else float(raw_wr)
    raw_pnl = block.get("emergency_session_pnl_gbp", default_pnl)
    emergency_pnl = default_pnl if raw_pnl is None else float(raw_pnl)
    if demo_soak:
        # Hard override — demo throughput must keep dispatching; size canary
        # remains the soak brake. max_daily_loss_gbp is a separate hard stop.
        emergency_wr = 0.0
        emergency_pnl = 0.0
    rolling_w = int(block.get("rolling_window", 20) or 20)
    rolling_min_n = int(block.get("rolling_min_sample", 8) or 8)
    rw, rl, rt, rwr = rolling_managed_win_rate(window=rolling_w, cfg=cfg)
    session_pnl = session_managed_pnl_gbp(cfg=cfg)
    value.update(
        {
            "rolling_win_rate": round(rwr, 4),
            "rolling_total": rt,
            "session_pnl_gbp": round(session_pnl, 2),
            "emergency_rolling_wr_floor": emergency_wr,
            "emergency_session_pnl_gbp": emergency_pnl,
        }
    )

    # emergency_wr <= 0 disables the WR emergency halt (demo soak).
    if emergency_wr > 0 and rt >= rolling_min_n and rwr < emergency_wr:
        return (
            False,
            f"emergency desk halt rolling WR {rwr:.0%} < {emergency_wr:.0%} ({rw}W/{rl}L last {rt})",
            value,
        )
    # emergency_pnl >= 0 disables the session PnL emergency halt (demo soak).
    # Phantom/managed-close accounting (e.g. broker_upl_hard_floor £k outliers)
    # must not false-halt entries under demo_throughput_mode.
    if emergency_pnl < 0 and session_pnl <= emergency_pnl:
        return (
            False,
            f"emergency desk halt session PnL £{session_pnl:.2f} <= £{emergency_pnl:.2f}",
            value,
        )
    return True, "desk halt clear", value


def canary_lot_for_epic(epic: str, cfg: Any | None = None) -> float:
    """Desk canary transmit size — size-up blocked until rolling WR proves."""
    block = _quality_block(cfg)
    overrides = block.get("canary_size_by_epic") if isinstance(block, dict) else None
    if isinstance(overrides, dict):
        for key, val in overrides.items():
            if key and key in str(epic or ""):
                try:
                    return max(0.01, float(val))
                except (TypeError, ValueError):
                    pass
    epic_u = str(epic or "").upper()
    if "CFPGOLD" in epic_u:
        return 10.0
    if "NIKKEI" in epic_u or "DOW" in epic_u or "FTSE" in epic_u or "DAX" in epic_u:
        return 0.5
    return float(block.get("canary_size_default", 0.5) or 0.5)


def clamp_size_until_rolling_wr(
    epic: str,
    size: float,
    *,
    cfg: Any | None = None,
) -> tuple[float, str]:
    """
    Cap transmit size at canary until rolling managed WR ≥ floor over N closes.

    Prevents size-up while the desk is still proving edge.
    """
    block = _quality_block(cfg)
    if not bool(block.get("enabled", False)):
        return float(size), "quality_off"
    min_wr = float(block.get("size_scale_min_wr", 0.55) or 0.55)
    min_n = int(block.get("size_scale_min_sample", 20) or 20)
    canary = canary_lot_for_epic(epic, cfg)
    requested = float(size)
    if requested <= canary + 1e-9:
        return requested, "at_or_below_canary"
    _w, _l, total, wr = rolling_managed_win_rate(window=max(min_n, 1), cfg=cfg)
    if total < min_n:
        return canary, f"size_capped_canary_n={total}_lt_{min_n}"
    if wr < min_wr:
        return canary, f"size_capped_canary_wr={wr:.2f}_lt_{min_wr:.2f}"
    return requested, f"size_scale_ok_wr={wr:.2f}_n={total}"


def evaluate_entry_slot_gate(
    cfg: Any | None = None,
    *,
    epic: str = "",
    direction: str = "",
    bid: float = 0.0,
    offer: float = 0.0,
) -> tuple[bool, str]:
    """Block entries outside configured intraday slots (e.g. europe_open, us_cash)."""
    try:
        import time

        from runtime.intraday_slot_tracker import intraday_slots_enabled, slot_id_for_timestamp

        if cfg is None:
            from system.config_loader import get_config

            cfg = get_config()
        if not intraday_slots_enabled(cfg):
            return True, "intraday slots off"
        block = cfg.get("intraday_slots") if hasattr(cfg, "get") else {}
        if not isinstance(block, dict):
            return True, "no slot config"
        allowed = block.get("entry_allowed_slots")
        if not isinstance(allowed, list) or not allowed:
            return True, "all slots allowed"
        slot_id = slot_id_for_timestamp(time.time(), cfg) or ""
        if slot_id in allowed:
            return True, f"slot {slot_id} allowed"
        # High-conviction sniper path bypasses slot windows (MEAN_REVERSION / us_close idle).
        if epic and direction and float(bid) > 0 and float(offer) > float(bid):
            from execution.pre_entry_regime_veto import sovereign_ml_obi_bypass_qualifies

            bypass_ok, bypass_detail = sovereign_ml_obi_bypass_qualifies(
                str(epic),
                str(direction),
                bid=float(bid),
                offer=float(offer),
                cfg=cfg,
            )
            if bypass_ok:
                return True, f"{bypass_detail} slot={slot_id}"
        return False, f"slot {slot_id} not in entry_allowed_slots {allowed}"
    except Exception:
        return True, "slot gate unavailable"


def _entry_hour_block(cfg: Any | None) -> dict[str, Any]:
    resolved = _resolve_cfg(cfg)
    if resolved is None:
        return {}
    raw = resolved.get("entry_hour_gate") if hasattr(resolved, "get") else None
    return raw if isinstance(raw, dict) else {}


def evaluate_entry_hour_gate(
    epic: str = "",
    *,
    cfg: Any | None = None,
    confidence: float | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Soft-block or size-cut DOW entries in historically bad London hours.

    Config-driven (``entry_hour_gate``). Does **not** reintroduce night blackout —
    night matrix / overnight slots remain allowed. Prefer-hours are advisory.
    """
    block = _entry_hour_block(cfg)
    meta: dict[str, Any] = {"enabled": bool(block.get("enabled", False))}
    if not block.get("enabled"):
        return True, "entry_hour_gate off", meta

    epics = block.get("epics") or ["IX.D.DOW.IFM.IP"]
    epic_s = str(epic or "").strip()
    if epic_s and epic_s not in epics:
        return True, "epic not in hour gate", meta

    try:
        tz_name = str(block.get("timezone") or "Europe/London")
        tz = ZoneInfo(tz_name)
        when = now.astimezone(tz) if now is not None else datetime.now(tz)
        hour = int(when.hour)
    except Exception:
        return True, "entry_hour_gate tz error", meta

    avoid = {int(h) for h in (block.get("avoid_hours") or [])}
    prefer = {int(h) for h in (block.get("prefer_hours") or [])}
    mode = str(block.get("mode") or "soft_block").lower()
    bypass = float(block.get("strong_signal_bypass_confidence") or 0.72)
    size_cut = float(block.get("size_cut_factor") or 0.5)
    meta.update(
        {
            "hour": hour,
            "avoid_hours": sorted(avoid),
            "prefer_hours": sorted(prefer),
            "mode": mode,
            "size_cut_factor": size_cut,
        }
    )

    if hour in prefer:
        return True, f"prefer_hour_{hour}", meta

    if hour not in avoid:
        return True, f"hour_{hour}_ok", meta

    conf = float(confidence) if confidence is not None else None
    if conf is not None and conf >= bypass:
        meta["bypassed"] = True
        return True, f"avoid_hour_{hour}_strong_signal_bypass conf={conf:.2f}", meta

    if mode == "size_cut":
        meta["size_cut"] = True
        # Soft pass — callers apply size_cut_factor via clamp helper.
        return True, f"avoid_hour_{hour}_size_cut", meta

    return False, f"avoid_hour_{hour}_soft_block", meta


def hour_gate_size_factor(
    epic: str = "",
    *,
    cfg: Any | None = None,
    confidence: float | None = None,
    now: datetime | None = None,
) -> float:
    """Return 1.0 or configured size_cut_factor when in avoid hour + size_cut mode."""
    ok, detail, meta = evaluate_entry_hour_gate(
        epic, cfg=cfg, confidence=confidence, now=now
    )
    if meta.get("size_cut") or "size_cut" in detail:
        return float(meta.get("size_cut_factor") or 0.5)
    _ = ok
    return 1.0


def current_slot_win_rate(cfg: Any | None = None) -> tuple[str, int, int, int, float]:
    """Return (slot_id, wins, losses, total, wr) for the active intraday slot."""
    try:
        import time

        from runtime.intraday_slot_tracker import intraday_slots_enabled, slot_id_for_timestamp, snapshot

        if not intraday_slots_enabled(cfg):
            return "", 0, 0, 0, 0.0
        slot_id = slot_id_for_timestamp(time.time(), cfg) or ""
        if not slot_id:
            return "", 0, 0, 0, 0.0
        body = snapshot(cfg=cfg)
        slot = (body.get("slots") or {}).get(slot_id) or {}
        current = slot.get("current") or {}
        wins = int(current.get("wins") or 0)
        n = int(current.get("n") or 0)
        losses = max(0, n - wins)
        wr = float(current.get("win_rate") or 0.0)
        return slot_id, wins, losses, n, wr
    except Exception:
        return "", 0, 0, 0, 0.0


def _resolve_session_win_rate(cfg: Any | None) -> tuple[int, int, int, float, str]:
    """Pick the best available WR source: managed closes preferred when learning DB is empty."""
    block = _quality_block(cfg)
    use_managed = bool(block.get("use_managed_closes", True))
    db_w, db_l, db_t, db_wr = session_labeled_win_rate(cfg=cfg)
    if use_managed or db_t < int(block.get("min_labeled_closes_before_gate", DEFAULT_MIN_LABELED)):
        m_w, m_l, m_t, m_wr = session_managed_win_rate(cfg=cfg)
        if m_t >= db_t:
            return m_w, m_l, m_t, m_wr, "managed_closes"
    return db_w, db_l, db_t, db_wr, "learning_db"


def evaluate_session_win_rate_gate(cfg: Any | None = None) -> tuple[bool, str, dict[str, Any]]:
    """
    Block new entries when today's win rate is below floor after min sample.

    Uses managed closes (trade_support / micro_gbp_exit) when the learning DB
    has no WIN/LOSS labels — fixes the blind gate that allowed 5% WR churn.

    Returns (passed, detail, value_dict).
    """
    block = _quality_block(cfg)
    if not bool(block.get("enabled", False)):
        return True, "strategy quality gate off", {"enabled": False}

    halt_ok, halt_detail, halt_value = evaluate_desk_halt_gate(cfg)
    if not halt_ok:
        return False, halt_detail, halt_value

    slot_ok, slot_detail = evaluate_entry_slot_gate(cfg)
    if not slot_ok:
        return False, slot_detail, {"entry_slot": slot_detail}

    hour_ok, hour_detail, hour_meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=cfg
    )
    if not hour_ok:
        return False, hour_detail, {"entry_hour": hour_meta}

    min_wr = float(block.get("min_session_win_rate", WIN_RATE_FLOOR))
    min_n = int(block.get("min_labeled_closes_before_gate", DEFAULT_MIN_LABELED))
    wins, losses, total, wr, source = _resolve_session_win_rate(cfg)

    rolling_w = int(block.get("rolling_window", 20) or 20)
    rolling_min_n = int(block.get("rolling_min_sample", 8) or 8)
    rolling_floor = float(block.get("rolling_win_rate_floor", 0.35) or 0.35)
    rw, rl, rt, rwr = rolling_managed_win_rate(window=rolling_w, cfg=cfg)

    loss_streak = consecutive_managed_loss_streak(cfg=cfg)
    streak_pause = int(block.get("loss_streak_pause", 5) or 5)

    slot_id, sw, sl, sn, swr = current_slot_win_rate(cfg=cfg)
    slot_min_n = int(block.get("slot_min_sample", 10) or 10)
    slot_floor = float(block.get("slot_win_rate_floor", 0.25) or 0.25)

    value = {
        "wins": wins,
        "losses": losses,
        "labeled_closes": total,
        "win_rate": round(wr, 4),
        "min_win_rate": min_wr,
        "min_sample": min_n,
        "source": source,
        "rolling": {
            "window": rolling_w,
            "wins": rw,
            "losses": rl,
            "total": rt,
            "win_rate": round(rwr, 4),
            "floor": rolling_floor,
        },
        "loss_streak": loss_streak,
        "current_slot": {
            "id": slot_id,
            "wins": sw,
            "losses": sl,
            "total": sn,
            "win_rate": round(swr, 4),
        },
    }

    demo_soak = _demo_throughput_enabled(cfg)
    value["demo_throughput_soak"] = demo_soak

    if bool(block.get("use_managed_closes", True)) and not demo_soak:
        # Demo soak: rolling WR / loss streak clamp size via canary, not entries.
        if loss_streak >= streak_pause > 0:
            return (
                False,
                f"loss_streak_pause {loss_streak} consecutive losses >= {streak_pause}",
                value,
            )

        if rt >= rolling_min_n and rwr < rolling_floor:
            return (
                False,
                f"rolling WR {rwr:.0%} < {rolling_floor:.0%} ({rw}W/{rl}L last {rt})",
                value,
            )

        if slot_id and sn >= slot_min_n and swr < slot_floor:
            return (
                False,
                f"slot {slot_id} WR {swr:.0%} < {slot_floor:.0%} ({sw}W/{sl}L)",
                value,
            )

    if total < min_n:
        return (
            True,
            f"session WR {wr:.0%} ({wins}W/{losses}L via {source}) — sample {total}/{min_n}",
            value,
        )
    if (not demo_soak) and wr < min_wr:
        return (
            False,
            f"session WR {wr:.0%} < {min_wr:.0%} floor ({wins}W/{losses}L today via {source})",
            value,
        )
    return (
        True,
        f"session WR {wr:.0%} >= {min_wr:.0%} ({wins}W/{losses}L via {source})",
        value,
    )


def profit_target_snapshot(cfg: Any | None = None) -> dict[str, Any]:
    block = _quality_block(cfg)
    target = float(block.get("target_daily_profit_gbp", DEFAULT_TARGET_GBP))
    capital = float(block.get("capital_gbp", DEFAULT_CAPITAL_GBP))
    try:
        from intelligence.target_engine import get_target_engine

        te = get_target_engine()
        p_day = float(getattr(te, "last_p_day", 0.0) or 0.0)
        return {
            "target_daily_gbp": target,
            "capital_gbp": capital,
            "realized_p_day_gbp": round(p_day, 2),
            "capital_preservation": bool(
                getattr(te, "capital_preservation", False)
                or getattr(te, "mission_accomplished", False)
            ),
        }
    except Exception:
        return {
            "target_daily_gbp": target,
            "capital_gbp": capital,
            "realized_p_day_gbp": 0.0,
            "capital_preservation": False,
        }
