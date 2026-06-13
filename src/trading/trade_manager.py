"""Open-trade management — breakeven, trailing, exits (config-driven)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from data.learning_store import LearningStore
from data.models import Quote, TradeRecord
from execution.trailing_stop_engine import BreakevenEval, TrailEval, eval_breakeven_stop, eval_trailing_stop
from system.config import Config
from system.engine_log import log_engine
from system.trade_lifecycle_bus import (
    STAGE_POSITION_TRACKING,
    STATUS_OK,
    get_lifecycle_bus,
)

if TYPE_CHECKING:
    from execution.stop_dispatch_worker import StopDispatchJob
    from trading.points_engine import PointsEngine

HARD_CAP_ATR_MULTIPLE = 3.0
PARTIAL_CLOSE_ATR_MULTIPLE = 1.5
_MAX_AGE_WARNED: set[int] = (
    set()
)  # trade IDs already warned to avoid repeat Telegram spam
PARTIAL_CLOSE_FRACTION = 0.5


class TradeManager:
    def __init__(
        self,
        config: Config,
        store: LearningStore,
        on_alert: Callable[[str], None] | None = None,
        *,
        skip_ig_synced_exits: bool = False,
        rest_client: Any | None = None,
        broker_stop_management: bool = False,
        points_engine: PointsEngine | None = None,
    ) -> None:
        self._cfg = config
        self.store = store
        self.on_alert = on_alert
        self._skip_ig_synced_exits = skip_ig_synced_exits
        self._rest = rest_client
        self._broker_stops = bool(broker_stop_management and rest_client is not None)
        self._points_engine = points_engine
        self._last_ig_stop: dict[str, float] = {}
        self._last_ig_limit: dict[str, float] = {}
        self._limit_ext_count: dict[int, int] = {}
        self._gone_deals: set[str] = set()
        self._capital_recycle_applied: set[int] = set()
        self._scalping_be_armed: set[int] = set()
        self._protect_active = False
        self._scalping_trail = False
        self._refresh_exec_protect_flags()
        if os.environ.get("IG_AGENT_SYNC_STOP_DISPATCH", "").strip() == "1":
            from execution.stop_dispatch_worker import configure_sync_mode

            configure_sync_mode(True)

    def _refresh_exec_protect_flags(self) -> None:
        try:
            from execution.execution_protect import is_protect_enabled
            from execution.scalping.config import is_scalping_enabled

            self._protect_active = is_protect_enabled(self._cfg)
            self._scalping_trail = is_scalping_enabled(self._cfg)
        except Exception:
            self._protect_active = False
            self._scalping_trail = False

    @staticmethod
    def confidence_band(confidence: float) -> str:
        if confidence >= 92.0:
            return "high"
        if confidence >= 85.0:
            return "standard"
        if confidence >= 80.0:
            return "marginal"
        return "low"

    @staticmethod
    def get_trail_distance(confidence: float, atr: float) -> float:
        """ATR-based trail distance fixed at entry by confidence band."""
        try:
            atr_v = float(atr)
            if atr_v <= 0:
                return 0.0
            conf = float(confidence)
            if conf >= 92.0:
                return 1.75 * atr_v
            if conf >= 85.0:
                return 1.50 * atr_v
            if conf >= 80.0:
                return 1.00 * atr_v
            return 1.50 * atr_v
        except Exception:
            try:
                return 1.50 * float(atr)
            except Exception:
                return 0.0

    @property
    def config(self) -> Config:
        return self._cfg

    def open_trade_from_execution(
        self,
        *,
        market: str,
        epic: str,
        side: str,
        quote: Quote,
        raw_confidence: float,
        adjusted_confidence: float,
        setup_key: str,
        deal_reference: str,
        notes: str,
        execution: dict[str, Any],
        dry_run: bool,
        ig_deal_id: str | None = None,
    ) -> int:
        cfg = self._cfg
        size = float(execution.get("size", cfg.trade_size))
        risk = float(execution.get("risk", cfg.stop_distance_points))
        limit = float(execution.get("limit", cfg.limit_distance_points))

        entry = float(quote.offer if side == "BUY" else quote.bid)
        stop = entry - risk if side == "BUY" else entry + risk
        target = entry + limit if side == "BUY" else entry - limit

        full_notes = (
            f"{notes} | adaptive execution: risk={risk:.1f}, "
            f"target_distance={limit:.1f}, size={size:.2f}, "
            f"{execution.get('notes', '')}"
        )

        existing = None
        if ig_deal_id:
            existing = self.store.find_open_by_deal_id(ig_deal_id)
        if existing is None and deal_reference:
            existing = self.store.find_open_by_deal_reference(deal_reference)
        if existing is not None:
            trade_id = int(existing["id"])
            self.store.update_protection_from_execution(
                trade_id,
                stop=stop,
                target=target,
                setup_key=setup_key,
                raw_confidence=raw_confidence,
                adjusted_confidence=adjusted_confidence,
                notes=f" | {full_notes}",
                deal_reference=deal_reference,
            )
            if ig_deal_id:
                self.store.set_ig_deal_id(trade_id, ig_deal_id)
            return trade_id

        extra = {"ig_deal_id": ig_deal_id} if ig_deal_id else None
        record = TradeRecord(
            id=None,
            market=market,
            epic=epic,
            side=side,
            entry=entry,
            exit=None,
            size=size,
            stop=stop,
            target=target,
            pnl_points=None,
            result=None,
            confidence=raw_confidence,
            adjusted_confidence=adjusted_confidence,
            setup_key=setup_key,
            dry_run=dry_run,
            deal_reference=deal_reference,
            notes=full_notes,
            extra=extra,
        )
        trade_id = self.store.open_trade(record)
        if ig_deal_id:
            self.store.set_ig_deal_id(trade_id, ig_deal_id)

        entry_atr = float(
            execution.get("atr")
            or execution.get("entry_atr")
            or execution.get("risk")
            or cfg.stop_distance_points
            or 0
        )
        band = str(
            execution.get("confidence_band")
            or self.confidence_band(adjusted_confidence)
        )
        trail_distance = float(
            execution.get("trail_distance")
            or self._resolve_trail_distance(adjusted_confidence, entry_atr, epic=epic)
        )
        if entry_atr > 0 and trail_distance > 0:
            self.store.set_v25_entry_meta(
                trade_id,
                confidence_band=band,
                entry_atr=entry_atr,
                trail_distance=trail_distance,
            )

        if not dry_run:
            try:
                from execution.japan225_daily_risk import record_trade_opened

                record_trade_opened(epic)
            except Exception:
                pass
            try:
                from feeder.event_bus import emit_fill_open

                pv = float(cfg.get("ig_point_value_gbp", 1.0))
                risk_gbp = float(stop) * float(size) * pv if stop and size else None
                emit_fill_open(
                    epic=epic,
                    market=market,
                    trade_id=int(trade_id),
                    deal_id=str(ig_deal_id or ""),
                    direction=side,
                    entry=float(entry),
                    size=float(size),
                    stop=float(stop),
                    target=float(target),
                    confidence=float(adjusted_confidence),
                    setup_key=str(setup_key or ""),
                    risk_gbp=risk_gbp,
                )
            except Exception:
                pass
        self._telegram_trade_opened(
            market=market,
            side=side,
            entry=entry,
            size=size,
            stop=stop,
            target=target,
            adjusted_confidence=adjusted_confidence,
            execution=execution,
        )
        return trade_id

    def update_from_hub_quote(self, epic: str, bid: float, offer: float) -> list[str]:
        """Lightweight hub-stream path — trailing/BE only, no dashboard coupling."""
        quote = Quote(datetime.utcnow(), float(bid), float(offer))
        return self.update_from_quote("", epic, quote, fast_path=True)

    def update_from_quote(
        self,
        market: str,
        epic: str,
        quote: Quote,
        *,
        fast_path: bool = False,
    ) -> list[str]:
        cfg = self._cfg
        messages: list[str] = []

        for tr in self.store.active_trades(epic):
            side = tr["side"]
            trade_id = int(tr["id"])
            keys = tr.keys()
            ig_deal = str(tr["ig_deal_id"] or "") if "ig_deal_id" in keys else ""
            entry = float(tr["entry"])
            stop = float(tr["stop"])
            target = float(tr["target"])
            size = float(tr["size"])
            px = quote.bid if side == "BUY" else quote.offer
            broker_managed = self._broker_stops and bool(ig_deal)
            adjusted_conf = float(tr["adjusted_confidence"])
            entry_atr, trail_distance, _band = self._entry_trail_meta(tr, adjusted_conf)

            if self._skip_ig_synced_exits and ig_deal and not broker_managed:
                continue

            hard_msg = self._check_hard_cap(
                market, side, trade_id, entry, stop, target, px, entry_atr, epic
            )
            if hard_msg:
                messages.extend(hard_msg)
                continue

            age_msg = self._check_max_position_age(
                market, side, trade_id, entry, px, ig_deal, epic, tr
            )
            if age_msg:
                messages.extend(age_msg)
                continue

            friday_msg = self._check_friday_close(
                market, side, trade_id, entry, px, ig_deal, epic, tr
            )
            if friday_msg:
                messages.extend(friday_msg)
                continue

            recycle_msgs: list[str] = []
            try:
                recycle_msgs = self._apply_capital_recycle_breakeven(
                    market,
                    side,
                    trade_id,
                    entry,
                    stop,
                    target,
                    px,
                    tr,
                )
            except Exception as e:
                log_engine(
                    f"capital_recycle skipped trade={trade_id} {market}: "
                    f"{type(e).__name__}: {e}"
                )
            if recycle_msgs:
                messages.extend(recycle_msgs)
                stop = self._current_stop(trade_id, stop)

            prev_stop = stop
            prev_target = target
            if cfg.partial_close_enabled:
                messages.extend(
                    self._apply_partial_close(
                        market,
                        side,
                        trade_id,
                        entry,
                        size,
                        px,
                        entry_atr,
                        adjusted_conf,
                        ig_deal,
                        epic,
                    )
                )
            row_after_partial = self.store.conn.execute(
                "SELECT size FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
            if row_after_partial:
                size = float(row_after_partial["size"])

            protect_active = self._protect_active
            scalping_trail = self._scalping_trail
            if not fast_path:
                self._refresh_exec_protect_flags()
                protect_active = self._protect_active
                scalping_trail = self._scalping_trail

            if protect_active and cfg.breakeven_enabled:
                messages.extend(
                    self._apply_scalping_breakeven_trail(
                        market,
                        side,
                        trade_id,
                        entry,
                        stop,
                        target,
                        px,
                        quote,
                        entry_atr,
                        epic=epic,
                        enable_atr_trail=scalping_trail,
                    )
                )
                stop = self._current_stop(trade_id, stop)
            elif cfg.breakeven_enabled:
                be_trigger = self._effective_breakeven_trigger(entry_atr, epic=epic)
                messages.extend(
                    self._apply_breakeven(
                        market,
                        side,
                        trade_id,
                        entry,
                        stop,
                        target,
                        px,
                        be_trigger,
                        cfg.breakeven_lock_points,
                        epic=epic,
                    )
                )
                stop = self._current_stop(trade_id, stop)

            if cfg.adaptive_trailing_stop_enabled:
                trail_dist = self._trail_distance_price(epic, trail_distance, cfg)
                trail_trigger = self._effective_trail_trigger(entry_atr, epic=epic)
                messages.extend(
                    self._apply_trailing(
                        market,
                        side,
                        trade_id,
                        entry,
                        stop,
                        target,
                        px,
                        trail_trigger,
                        trail_dist,
                        epic=epic,
                    )
                )
                stop = self._current_stop(trade_id, stop)

            if getattr(cfg, "limit_extension_enabled", False) and entry_atr > 0:
                ext_msgs, target = self._apply_limit_extension(
                    market, side, trade_id, entry, target, px, entry_atr, epic=epic
                )
                messages.extend(ext_msgs)

            if broker_managed:
                tol = self._stop_tolerance(epic)
                stop_moved = abs(stop - prev_stop) >= tol
                limit_moved = abs(target - prev_target) >= tol
                new_limit = target if limit_moved else None
                if stop_moved or limit_moved:
                    pushed = self._sync_stop_to_ig(
                        ig_deal,
                        trade_id=trade_id,
                        side=side,
                        stop=stop,
                        epic=epic,
                        new_limit=new_limit,
                        fast_path=fast_path,
                    )
                    if pushed and stop_moved and not fast_path:
                        msg = (
                            messages[-1]
                            if messages
                            else f"IG stop synced stop={stop:.1f}"
                        )
                        get_lifecycle_bus().emit(
                            STAGE_POSITION_TRACKING,
                            STATUS_OK,
                            msg,
                            deal_id=ig_deal,
                            stop=stop,
                        )
                continue

            hit, exit_price = self._check_exit(side, entry, stop, target, px)
            if hit and exit_price is not None:
                from system.pnl_math import classify_result, realised_pnl_points

                pnl = realised_pnl_points(side, entry, exit_price)
                hit = classify_result(pnl)
                pts_before = (
                    float(self._points_engine._cumulative)
                    if self._points_engine is not None
                    else None
                )
                self.store.close_trade(
                    trade_id,
                    exit_price,
                    pnl,
                    hit,
                    f"Closed on {hit} at {exit_price:.1f}; target was {target:.1f}",
                )
                get_lifecycle_bus().mark_position_closed(
                    message=f"Bot closed {hit}",
                    result=hit,
                    pnl=pnl,
                    source="bot",
                )
                self._telegram_trade_closed(
                    trade_id,
                    exit_price=exit_price,
                    pnl_pts=pnl,
                    result=hit,
                    points_before=pts_before,
                )
                msg = (
                    f"TRADE CLOSED {hit} | {market} {side} | entry {entry:.1f} "
                    f"exit {exit_price:.1f} | {pnl:.1f} pts"
                )
                messages.append(msg)
                if self.on_alert:
                    self.on_alert(msg)

        return messages

    def _telegram_trade_opened(
        self,
        *,
        market: str,
        side: str,
        entry: float,
        size: float,
        stop: float,
        target: float,
        adjusted_confidence: float,
        execution: dict[str, Any],
    ) -> None:
        try:
            from system.telegram_notifier import get_telegram_notifier

            notifier = get_telegram_notifier()
            if notifier is None or not notifier.enabled:
                return
            fitness = float(execution.get("fitness_score") or 0)
            pts_state = "CAUTION"
            if self._points_engine is not None:
                pts_state = self._points_engine.get_state()
            protect_on = bool(execution.get("execution_protect", False))
            order_type = str(execution.get("order_type") or "MARKET")
            protected = bool(execution.get("protection_verified", True))
            notifier.notify_trade_opened(
                market=market,
                direction=side,
                entry=entry,
                size=size,
                stop=stop,
                target=target,
                signal_pct=float(adjusted_confidence),
                fitness_pct=fitness,
                points_state=pts_state,
                order_type=order_type,
                protected=protected,
                scalping=protect_on,
            )
        except Exception as e:
            log_engine(f"telegram trade open notify failed: {type(e).__name__}: {e}")

    def _telegram_trade_closed(
        self,
        trade_id: int,
        *,
        exit_price: float,
        pnl_pts: float,
        result: str,
        points_before: float | None = None,
    ) -> None:
        try:
            from system.telegram_notifier import get_telegram_notifier

            notifier = get_telegram_notifier()
            if notifier is None or not notifier.enabled:
                return
            row = self.store.conn.execute(
                "SELECT market, side, entry, opened_at, ig_pnl_currency, "
                "adjusted_confidence FROM trades WHERE id=?",
                (trade_id,),
            ).fetchone()
            if row is None:
                return
            market = str(row["market"] or "")
            side = str(row["side"] or "")
            entry = float(row["entry"] or 0)
            opened_at = row["opened_at"]
            duration_mins: float | None = None
            if opened_at:
                try:
                    opened = datetime.fromisoformat(
                        str(opened_at).replace("Z", "+00:00")
                    )
                    if opened.tzinfo is not None:
                        opened = opened.replace(tzinfo=None)
                    duration_mins = max(
                        0.0, (datetime.now() - opened).total_seconds() / 60.0
                    )
                except (TypeError, ValueError):
                    duration_mins = None
            ig_pnl = row["ig_pnl_currency"]
            pnl_gbp = float(ig_pnl) if ig_pnl is not None else None
            pts_after: float | None = None
            state = "CAUTION"
            if self._points_engine is not None:
                pts_after = float(self._points_engine._cumulative)
                state = self._points_engine.get_state()
            notifier.notify_trade_closed(
                market=market,
                direction=side,
                entry=entry,
                exit_price=exit_price,
                pnl_gbp=pnl_gbp,
                pnl_pts=float(pnl_pts),
                duration_mins=duration_mins,
                points_before=points_before,
                points_after=pts_after,
                points_state=state,
            )
        except Exception as e:
            log_engine(f"telegram trade close notify failed: {type(e).__name__}: {e}")

    def _current_stop(self, trade_id: int, fallback: float) -> float:
        stop = self.store.get_stop(trade_id)
        return stop if stop is not None else fallback

    def _award_protection_milestone(
        self, trade_id: int, kind: str, market: str
    ) -> float:
        """Once-per-trade bonus when BE, trail, or limit extension fires."""
        if self._points_engine is None:
            return 0.0
        marker = f"PTS_MILESTONE:{kind}"
        row = self.store.conn.execute(
            "SELECT notes FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if row and marker in str(row["notes"] or ""):
            return 0.0
        try:
            score = self._points_engine.record_milestone(
                kind, market=market, trade_id=trade_id
            )
        except Exception as e:
            log_engine(
                f"protection milestone score failed trade={trade_id} "
                f"{kind}: {type(e).__name__}: {e}"
            )
            return 0.0
        if score > 0:
            self.store.conn.execute(
                "UPDATE trades SET notes=COALESCE(notes,'') || ? WHERE id=?",
                (f" | {marker}", trade_id),
            )
            self.store.conn.commit()
        return score

    def _entry_trail_meta(
        self, tr: Any, adjusted_confidence: float
    ) -> tuple[float, float, str]:
        keys = tr.keys()
        epic = str(tr["epic"] or "") if "epic" in keys else ""
        entry_atr = (
            float(tr["entry_atr"])
            if "entry_atr" in keys and tr["entry_atr"] is not None
            else 0.0
        )
        trail_distance = (
            float(tr["trail_distance"])
            if "trail_distance" in keys and tr["trail_distance"] is not None
            else 0.0
        )
        band = (
            str(tr["confidence_band"])
            if "confidence_band" in keys and tr["confidence_band"]
            else self.confidence_band(adjusted_confidence)
        )
        if trail_distance <= 0 and entry_atr > 0:
            trail_distance = self._resolve_trail_distance(
                adjusted_confidence, entry_atr, epic=epic
            )
        return entry_atr, trail_distance, band

    def _resolve_trail_distance(
        self, confidence: float, entry_atr: float, *, epic: str = ""
    ) -> float:
        if entry_atr > 0 and epic:
            try:
                from trading.trail_config import get_trail_overrides_for_epic

                overrides = get_trail_overrides_for_epic(epic)
                dist_mult = overrides.get("trail_distance_atr_multiple")
                if dist_mult is not None:
                    return float(dist_mult) * entry_atr
            except Exception:
                pass
        return self.get_trail_distance(confidence, entry_atr)

    def _profit_ig_points(self, side: str, entry: float, px: float, epic: str) -> float:
        """Unrealized move in IG dashboard points (pips for FX)."""
        from system.pnl_math import price_delta_to_ig_points, realised_pnl_points

        return price_delta_to_ig_points(epic, realised_pnl_points(side, entry, px))

    def _profit_points(
        self, side: str, entry: float, px: float, epic: str = ""
    ) -> float:
        return self._profit_ig_points(side, entry, px, epic)

    def _offset_price(self, epic: str, offset_pts: float) -> float:
        from system.pnl_math import ig_points_to_price_delta, pip_size_for_epic

        if pip_size_for_epic(epic) is not None:
            return ig_points_to_price_delta(epic, float(offset_pts))
        return float(offset_pts)

    def _trail_distance_price(
        self, epic: str, trail_distance: float, cfg: Config
    ) -> float:
        """Trail distance as a price delta for stop placement."""
        from system.pnl_math import ig_points_to_price_delta, pip_size_for_epic

        if trail_distance > 0:
            if pip_size_for_epic(epic) is not None and trail_distance >= 1.0:
                return ig_points_to_price_delta(epic, trail_distance)
            return float(trail_distance)
        step = float(cfg.trailing_stop_step_points)
        if pip_size_for_epic(epic) is not None:
            return ig_points_to_price_delta(epic, step)
        return step

    def _fallback_trigger_ig_points(
        self, epic: str, config_key: str, default: float
    ) -> float:
        from system.pnl_math import pip_size_for_epic

        fallback = float(getattr(self._cfg, config_key, default) or default)
        if pip_size_for_epic(epic) is None:
            return fallback
        try:
            from execution.trade_risk import configured_stop_points

            stop_pts = configured_stop_points(epic, self._cfg)
            if config_key == "breakeven_trigger_points":
                return min(fallback, max(1.0, stop_pts * 0.4))
            return min(fallback, max(1.0, stop_pts * 0.75))
        except Exception:
            return fallback

    def _trade_age_minutes(self, tr: Any) -> float | None:
        opened_at_raw = tr["opened_at"] if "opened_at" in tr.keys() else None
        if not opened_at_raw:
            return None
        try:
            opened_dt = datetime.fromisoformat(str(opened_at_raw).replace("Z", ""))
            return (datetime.utcnow() - opened_dt).total_seconds() / 60.0
        except Exception:
            return None

    def _apply_capital_recycle_breakeven(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        stop: float,
        target: float,
        px: float,
        tr: Any,
    ) -> list[str]:
        """Move stop to BE+spread lock on stale sideways trades in low-vol regimes."""
        try:
            return self._apply_capital_recycle_breakeven_impl(
                market,
                side,
                trade_id,
                entry,
                stop,
                target,
                px,
                tr,
            )
        except Exception as e:
            log_engine(
                f"capital_recycle error trade={trade_id} {market}: "
                f"{type(e).__name__}: {e}"
            )
            return []

    def _apply_capital_recycle_breakeven_impl(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        stop: float,
        target: float,
        px: float,
        tr: Any,
    ) -> list[str]:
        """Internal capital recycle — must not raise (caller also guarded)."""
        if not self._cfg.get("capital_recycle_enabled", True):
            return []
        if trade_id in self._capital_recycle_applied:
            return []
        age_mins = self._trade_age_minutes(tr)
        if age_mins is None:
            return []
        min_age = float(
            self._cfg.get(
                "capital_recycle_age_minutes",
                45,
            )
        )
        lock_pts = float(
            self._cfg.get(
                "capital_recycle_breakeven_lock_points",
                1.0,
            )
        )
        from execution.adaptive_engine import (
            capital_recycle_breakeven_stop,
            capital_recycle_eligible,
        )

        if not capital_recycle_eligible(
            age_minutes=age_mins,
            side=side,
            entry=entry,
            stop=stop,
            target=target,
            px=px,
            market=market,
            min_age_minutes=min_age,
        ):
            return []
        be_stop = capital_recycle_breakeven_stop(side, entry, lock_pts)
        if side == "BUY" and stop >= be_stop:
            return []
        if side == "SELL" and stop <= be_stop:
            return []
        self.store.update_stop(
            trade_id,
            be_stop,
            f" | Capital recycle BE+{lock_pts:.0f} ({age_mins:.0f}m low-vol)",
        )
        self._capital_recycle_applied.add(trade_id)
        msg = (
            f"CAPITAL RECYCLE | {market} {side} | stop → {be_stop:.1f} "
            f"({age_mins:.0f}m sideways, low vol)"
        )
        log_engine(msg)
        if self.on_alert:
            self.on_alert(msg)
        return [msg]

    def _check_max_position_age(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        px: float,
        ig_deal: str,
        epic: str,
        tr: Any,
    ) -> list[str]:
        """Force-close a position that has been open longer than max_position_age_minutes."""
        max_age = getattr(self._cfg, "max_position_age_minutes", None)
        if not max_age or max_age <= 0:
            return []
        opened_at_raw = tr["opened_at"] if "opened_at" in tr.keys() else None
        if not opened_at_raw:
            return []
        try:
            opened_dt = datetime.fromisoformat(str(opened_at_raw).replace("Z", ""))
            age_mins = (datetime.utcnow() - opened_dt).total_seconds() / 60.0
        except Exception:
            return []
        if age_mins < float(max_age):
            if trade_id in _MAX_AGE_WARNED:
                _MAX_AGE_WARNED.discard(trade_id)
            return []
        # Warn once at 80% of max age
        warn_threshold = float(max_age) * 0.8
        if age_mins >= warn_threshold and trade_id not in _MAX_AGE_WARNED:
            _MAX_AGE_WARNED.add(trade_id)
            warn_msg = (
                f"⏰ Position age warning: {market} {side} open {age_mins:.0f}m "
                f"(limit {max_age}m) — will auto-close at {max_age}m"
            )
            log_engine(warn_msg)
            self._telegram_alert(warn_msg)
        if age_mins < float(max_age):
            return []
        from system.pnl_math import classify_result, realised_pnl_points

        exit_price = px
        pnl = realised_pnl_points(side, entry, exit_price)
        hit = classify_result(pnl)
        pts_before = (
            float(self._points_engine._cumulative)
            if self._points_engine is not None
            else None
        )
        self.store.close_trade(
            trade_id,
            exit_price,
            pnl,
            hit,
            f"Max age {max_age}m exceeded ({age_mins:.0f}m open)",
        )
        self._telegram_trade_closed(
            trade_id,
            exit_price=exit_price,
            pnl_pts=pnl,
            result=hit,
            points_before=pts_before,
        )
        if ig_deal and self._rest is not None and hasattr(self._rest, "close_position"):
            try:
                self._rest.close_position(ig_deal, side=side, size=float(tr["size"]))
            except Exception as e:
                log_engine(f"Max-age IG close failed for {ig_deal}: {e}")
        msg = (
            f"MAX AGE CLOSE {hit} | {market} {side} | entry {entry:.1f} "
            f"exit {exit_price:.1f} | {pnl:.1f} pts | open {age_mins:.0f}m"
        )
        log_engine(msg)
        if self.on_alert:
            self.on_alert(msg)
        _MAX_AGE_WARNED.discard(trade_id)
        return [msg]

    @staticmethod
    def _is_friday_close_window() -> bool:
        """True from Friday 20:30 UTC — all open positions must be closed before weekly gap."""
        now = datetime.utcnow()
        return now.weekday() == 4 and (now.hour * 60 + now.minute) >= 20 * 60 + 30

    def _check_friday_close(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        px: float,
        ig_deal: str,
        epic: str,
        tr: Any,
    ) -> list[str]:
        """Force-close all positions at Friday 20:30 UTC to avoid weekend gap risk."""
        if not self._is_friday_close_window():
            return []
        from system.pnl_math import classify_result, realised_pnl_points

        exit_price = px
        pnl = realised_pnl_points(side, entry, exit_price)
        hit = classify_result(pnl)
        pts_before = (
            float(self._points_engine._cumulative)
            if self._points_engine is not None
            else None
        )
        self.store.close_trade(
            trade_id,
            exit_price,
            pnl,
            hit,
            "Friday 20:30 UTC auto-close (weekend gap protection)",
        )
        self._telegram_trade_closed(
            trade_id,
            exit_price=exit_price,
            pnl_pts=pnl,
            result=hit,
            points_before=pts_before,
        )
        if ig_deal and self._rest is not None and hasattr(self._rest, "close_position"):
            try:
                self._rest.close_position(ig_deal, side=side, size=float(tr["size"]))
            except Exception as e:
                log_engine(f"Friday auto-close IG REST failed for {ig_deal}: {e}")
        msg = (
            f"FRIDAY AUTO-CLOSE {hit} | {market} {side} | entry {entry:.1f} "
            f"exit {exit_price:.1f} | {pnl:.1f} pts | weekend gap protection"
        )
        log_engine(msg)
        self._telegram_alert(f"📅 {msg}")
        if self.on_alert:
            self.on_alert(msg)
        return [msg]

    def _telegram_alert(self, msg: str) -> None:
        try:
            from system.telegram_notifier import get_telegram_notifier

            n = get_telegram_notifier()
            if n:
                n.send_alert(msg)
        except Exception:
            pass

    def _telegram_scalping_event(self, msg: str, *, dedupe_key: str) -> None:
        """Scalping lifecycle (BE/trail) — deduped; suppressed in executive mode."""
        try:
            from system.telegram_notifier import get_telegram_notifier

            n = get_telegram_notifier()
            if n:
                n.send_alert(msg, dedupe_key=dedupe_key)
        except Exception:
            pass

    def _check_hard_cap(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        stop: float,
        target: float,
        px: float,
        entry_atr: float,
        epic: str,
    ) -> list[str]:
        if entry_atr <= 0:
            return []
        profit = self._profit_ig_points(side, entry, px, epic)
        from system.pnl_math import price_delta_to_ig_points

        atr_pts = price_delta_to_ig_points(epic, entry_atr)
        if profit < HARD_CAP_ATR_MULTIPLE * atr_pts:
            return []
        from system.pnl_math import classify_result, realised_pnl_points

        exit_price = px
        pnl = realised_pnl_points(side, entry, exit_price)
        hit = classify_result(pnl)
        pts_before = (
            float(self._points_engine._cumulative)
            if self._points_engine is not None
            else None
        )
        self.store.close_trade(
            trade_id,
            exit_price,
            pnl,
            hit,
            f"Hard cap +{HARD_CAP_ATR_MULTIPLE:.1f}x ATR at {exit_price:.1f}",
        )
        self._telegram_trade_closed(
            trade_id,
            exit_price=exit_price,
            pnl_pts=pnl,
            result=hit,
            points_before=pts_before,
        )
        msg = (
            f"HARD CAP EXIT {hit} | {market} {side} | entry {entry:.1f} "
            f"exit {exit_price:.1f} | {pnl:.1f} pts (+{HARD_CAP_ATR_MULTIPLE:.1f}x ATR)"
        )
        log_engine(msg)
        if self.on_alert:
            self.on_alert(msg)
        return [msg]

    def _apply_partial_close(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        size: float,
        px: float,
        entry_atr: float,
        adjusted_confidence: float,
        ig_deal: str,
        epic: str,
    ) -> list[str]:
        if not self._cfg.partial_close_enabled:
            return []
        if entry_atr <= 0 or size <= 0:
            return []
        if self.store.is_partial_close_done(trade_id):
            return []
        profit = self._profit_ig_points(side, entry, px, epic)
        from system.pnl_math import price_delta_to_ig_points

        atr_pts = price_delta_to_ig_points(epic, entry_atr)
        if profit < PARTIAL_CLOSE_ATR_MULTIPLE * atr_pts:
            return []

        half_size = size * PARTIAL_CLOSE_FRACTION
        if half_size <= 0:
            return []

        if ig_deal and self._rest is not None and hasattr(self._rest, "close_position"):
            try:
                self._rest.close_position(
                    ig_deal,
                    direction=side,
                    size=half_size,
                    epic=epic,
                    verify=False,
                )
            except Exception as e:
                log_engine(
                    f"Partial close broker failed deal={ig_deal}: {type(e).__name__}: {e}"
                )
                return []

        from system.pnl_math import classify_result, realised_pnl_points

        unit_pnl = realised_pnl_points(side, entry, px)
        banked_pts = unit_pnl * (half_size / size)
        result = classify_result(banked_pts)

        self.store.update_trade_size(trade_id, size - half_size)
        self.store.mark_partial_close_done(trade_id)
        note = f" | Partial close 50% at {px:.1f} banked {banked_pts:.1f} pts"
        self.store.conn.execute(
            "UPDATE trades SET notes=COALESCE(notes,'') || ? WHERE id=?",
            (note, trade_id),
        )
        self.store.conn.commit()

        if self._points_engine is not None:
            try:
                self._points_engine.record_trade(
                    result, adjusted_confidence, banked_pts
                )
            except Exception as e:
                log_engine(
                    f"points_engine partial close score failed: {type(e).__name__}: {e}"
                )

        msg = (
            f"PARTIAL CLOSE | {market} | {side} | 50% at {px:.1f} | "
            f"{banked_pts:.1f} pts banked"
        )
        log_engine(msg)
        if self.on_alert:
            self.on_alert(msg)
        return [msg]

    def _effective_trail_trigger(
        self, entry_atr: float, *, epic: str | None = None
    ) -> float:
        """ATR-scaled trailing trigger in IG points (pips for FX)."""
        epic_str = str(epic or "")
        mult = float(getattr(self._cfg, "trail_trigger_atr_multiple", 0.0))
        if epic_str:
            try:
                from trading.trail_config import get_trail_overrides_for_epic

                overrides = get_trail_overrides_for_epic(epic_str)
                if overrides.get("trail_trigger_atr_multiple") is not None:
                    mult = float(overrides["trail_trigger_atr_multiple"])
            except Exception:
                pass
        if mult > 0 and entry_atr > 0:
            from system.pnl_math import price_delta_to_ig_points

            return price_delta_to_ig_points(epic_str, mult * entry_atr)
        return self._fallback_trigger_ig_points(
            epic_str, "trailing_stop_trigger_points", 50.0
        )

    def _effective_breakeven_trigger(
        self, entry_atr: float, *, epic: str | None = None
    ) -> float:
        """ATR-scaled breakeven trigger in IG points (pips for FX)."""
        epic_str = str(epic or "")
        mult = float(getattr(self._cfg, "breakeven_trigger_atr_multiple", 0.0))
        if mult > 0 and entry_atr > 0:
            from system.pnl_math import price_delta_to_ig_points

            return price_delta_to_ig_points(epic_str, mult * entry_atr)
        return self._fallback_trigger_ig_points(
            epic_str, "breakeven_trigger_points", 30.0
        )

    def _apply_limit_extension(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        current_target: float,
        px: float,
        entry_atr: float,
        *,
        epic: str = "",
    ) -> tuple[list[str], float]:
        """
        Extend take-profit limit when trade trends strongly beyond the trigger threshold.

        Each extension pushes the limit by ``limit_extension_step_atr_multiple × ATR``.
        At most ``limit_extension_max_extensions`` extensions fire per position (in-memory counter).
        Returns (messages, new_target). new_target == current_target when no extension fires.
        """
        cfg = self._cfg
        if entry_atr <= 0:
            return [], current_target

        trigger_mult = float(getattr(cfg, "limit_extension_trigger_atr_multiple", 1.5))
        step_mult = float(getattr(cfg, "limit_extension_step_atr_multiple", 1.0))
        max_ext = int(getattr(cfg, "limit_extension_max_extensions", 2))
        step_price = step_mult * entry_atr

        ext_count = self._limit_ext_count.get(trade_id, 0)
        if ext_count >= max_ext:
            return [], current_target

        profit = self._profit_ig_points(side, entry, px, epic)
        from system.pnl_math import price_delta_to_ig_points

        required_price = (trigger_mult + ext_count * step_mult) * entry_atr
        required = price_delta_to_ig_points(epic, required_price)
        if profit < required:
            return [], current_target

        new_target = (
            current_target + step_price
            if side == "BUY"
            else current_target - step_price
        )

        self.store.update_target(
            trade_id,
            new_target,
            f" | Limit extended to {new_target:.1f} (ext #{ext_count + 1})",
        )
        self._limit_ext_count[trade_id] = ext_count + 1

        msg = (
            f"LIMIT EXTENDED | {market} {side} | "
            f"target {current_target:.1f} → {new_target:.1f} | "
            f"profit={profit:.1f} pts ext #{ext_count + 1}/{max_ext}"
        )
        log_engine(msg)
        if self.on_alert:
            self.on_alert(msg)
        self._award_protection_milestone(trade_id, "limit_extension", market)
        return [msg], new_target

    def _apply_scalping_breakeven_trail(
        self,
        market: str,
        side: str,
        trade_id: int,
        entry: float,
        stop: float,
        target: float,
        px: float,
        quote: Quote,
        entry_atr: float,
        *,
        epic: str = "",
        enable_atr_trail: bool = False,
    ) -> list[str]:
        """Micro-milestone BE (spread+commission+2pts) then optional ATR trail."""
        from execution.scalping.breakeven_trail import (
            breakeven_stop_offset,
            breakeven_trigger_points,
            trail_distance_from_atr,
        )

        msgs: list[str] = []
        be_trigger = breakeven_trigger_points(quote, self._cfg, epic=epic)
        be_offset = breakeven_stop_offset(quote, self._cfg, epic=epic)
        profit = self._profit_ig_points(side, entry, px, epic)

        if trade_id not in self._scalping_be_armed:
            if side == "BUY":
                be_stop = entry + be_offset
                if profit >= be_trigger and stop < be_stop:
                    self.store.update_stop(
                        trade_id,
                        be_stop,
                        f" | Scalping BE+tx {be_stop:.2f}",
                    )
                    self._scalping_be_armed.add(trade_id)
                    msgs.append(
                        f"EXEC_PROTECT BREAKEVEN | {market} BUY | stop {be_stop:.2f}"
                    )
                    log_engine(
                        f"EXEC_PROTECT breakeven armed trade={trade_id} "
                        f"trigger={be_trigger:.2f} stop={be_stop:.2f}"
                    )
                    self._telegram_scalping_event(
                        f"BE locked | {market} BUY → {be_stop:.2f}",
                        dedupe_key=f"protect_be:{market}:{trade_id}",
                    )
                    self._award_protection_milestone(trade_id, "breakeven", market)
            else:
                be_stop = entry - be_offset
                if profit >= be_trigger and stop > be_stop:
                    self.store.update_stop(
                        trade_id,
                        be_stop,
                        f" | Scalping BE+tx {be_stop:.2f}",
                    )
                    self._scalping_be_armed.add(trade_id)
                    msgs.append(
                        f"EXEC_PROTECT BREAKEVEN | {market} SELL | stop {be_stop:.2f}"
                    )
                    log_engine(
                        f"EXEC_PROTECT breakeven armed trade={trade_id} "
                        f"trigger={be_trigger:.2f} stop={be_stop:.2f}"
                    )
                    self._telegram_scalping_event(
                        f"BE locked | {market} SELL → {be_stop:.2f}",
                        dedupe_key=f"protect_be:{market}:{trade_id}",
                    )
                    self._award_protection_milestone(trade_id, "breakeven", market)

        if enable_atr_trail and trade_id in self._scalping_be_armed:
            atr = entry_atr if entry_atr > 0 else be_trigger
            trail_dist_raw = trail_distance_from_atr(atr, self._cfg)
            if trail_dist_raw > 0:
                if entry_atr > 0:
                    trail_dist = trail_dist_raw
                else:
                    trail_dist = self._trail_distance_price(
                        epic, trail_dist_raw, self._cfg
                    )
                msgs.extend(
                    self._apply_trailing(
                        market,
                        side,
                        trade_id,
                        entry,
                        self._current_stop(trade_id, stop),
                        target,
                        px,
                        be_trigger,
                        trail_dist,
                        epic=epic,
                    )
                )
        return msgs

    def _apply_breakeven(
        self,
        market,
        side,
        trade_id,
        entry,
        stop,
        target,
        px,
        trigger,
        offset,
        *,
        epic: str = "",
    ):
        msgs: list[str] = []
        profit = self._profit_ig_points(side, entry, px, epic)
        offset_price = self._offset_price(epic, offset)
        be_stop = eval_breakeven_stop(
            BreakevenEval(side, entry, stop, target, px, profit, trigger, offset_price)
        )
        if be_stop is None:
            return msgs
        self.store.update_stop(
            trade_id, be_stop, f" | Stop moved to breakeven {be_stop:.5f}"
        )
        if side == "BUY":
            msgs.append(f"BREAKEVEN STOP MOVED | {market} BUY | stop {be_stop:.5f}")
        else:
            msgs.append(f"BREAKEVEN STOP MOVED | {market} SELL | stop {be_stop:.5f}")
        self._award_protection_milestone(trade_id, "breakeven", market)
        return msgs

    def _apply_trailing(
        self,
        market,
        side,
        trade_id,
        entry,
        stop,
        target,
        px,
        trigger,
        distance,
        *,
        epic: str = "",
    ):
        msgs: list[str] = []
        profit = self._profit_ig_points(side, entry, px, epic)
        trail_stop = eval_trailing_stop(
            TrailEval(side, entry, stop, target, px, profit, trigger, distance)
        )
        if trail_stop is not None:
            from execution.dealing_constraints import (
                clamp_stop_to_broker_minimum,
                fetch_min_stop_points,
            )

            min_pts = fetch_min_stop_points(self._rest, epic)
            trail_stop = clamp_stop_to_broker_minimum(
                side,
                px=px,
                stop=trail_stop,
                min_distance_points=min_pts,
                epic=epic,
            )
        if trail_stop is None:
            if profit >= trigger:
                if side == "BUY" and (px - distance) < stop:
                    log_engine(
                        f"ERROR: Trail would move stop backwards — rejected. "
                        f"current={stop} proposed={px - distance:.5f}"
                    )
                elif side == "SELL" and (px + distance) > stop:
                    log_engine("ERROR: Trail would move stop backwards — rejected.")
            return msgs
        self.store.update_stop(
            trade_id,
            trail_stop,
            f" | Trailing stop {'raised' if side == 'BUY' else 'lowered'} to {trail_stop:.5f}",
        )
        if side == "BUY":
            msgs.append(
                f"TRAILING STOP RAISED | {market} BUY | stop {trail_stop:.5f}"
            )
        else:
            msgs.append(
                f"TRAILING STOP LOWERED | {market} SELL | stop {trail_stop:.5f}"
            )
        self._award_protection_milestone(trade_id, "trail", market)
        return msgs

    def _stop_tolerance(self, epic: str) -> float:
        from system.pnl_math import pip_size_for_epic

        return 0.00001 if pip_size_for_epic(epic) is not None else 0.05

    def _round_stop_level(self, level: float, epic: str) -> float:
        from system.pnl_math import pip_size_for_epic

        if pip_size_for_epic(epic) is not None:
            return round(float(level), 5)
        return round(float(level), 1)

    def _ig_position_levels(self, deal_id: str) -> tuple[float | None, float | None]:
        client = self._rest
        if client is None:
            return None, None
        rows: list[dict[str, Any]] = []
        if hasattr(client, "find_open_position"):
            row = client.find_open_position(deal_id)
            if row:
                rows.append(row)
        if not rows and hasattr(client, "open_positions"):
            for item in client.open_positions():
                pos = item.get("position") or {}
                if str(pos.get("dealId") or pos.get("dealID") or "") == deal_id:
                    rows.append(item)
                    break
        if not rows:
            return None, None
        pos = rows[0].get("position") or {}
        stop = float(pos.get("stopLevel") or 0)
        limit = float(pos.get("limitLevel") or 0)
        return (stop if stop > 0 else None, limit if limit > 0 else None)

    def _ig_stop_level(self, deal_id: str) -> float | None:
        stop, _ = self._ig_position_levels(deal_id)
        return stop

    def _ig_position_open(self, deal_id: str) -> bool:
        client = self._rest
        if client is None or not deal_id:
            return False
        if deal_id in self._gone_deals:
            return False
        if hasattr(client, "find_open_position"):
            return client.find_open_position(deal_id) is not None
        return self._ig_position_levels(deal_id) != (None, None)

    def _close_local_trade_position_gone(
        self,
        *,
        trade_id: int,
        deal_id: str,
        side: str,
        epic: str,
    ) -> None:
        if deal_id:
            self._gone_deals.add(deal_id)
        self._last_ig_stop.pop(f"{deal_id}:{trade_id}", None)

        row = self.store.conn.execute(
            "SELECT entry, closed_at FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if row is None or row["closed_at"]:
            return

        entry = float(row["entry"] or 0)
        self.store.close_trade(
            trade_id,
            entry,
            0.0,
            "UNKNOWN",
            notes=f"IG sync: position gone at broker (HTTP 404) deal={deal_id}",
        )
        log_engine(
            f"IG position gone — closed local trade id={trade_id} epic={epic} deal={deal_id}"
        )

    def _execute_stop_dispatch_job(self, job: StopDispatchJob) -> bool:
        return self._execute_stop_sync(
            job.deal_id,
            trade_id=job.trade_id,
            side=job.side,
            stop=job.stop,
            epic=job.epic,
            new_limit=job.new_limit,
        )

    def _sync_stop_to_ig(
        self,
        deal_id: str,
        *,
        trade_id: int,
        side: str,
        stop: float,
        epic: str,
        new_limit: float | None = None,
        fast_path: bool = False,
    ) -> bool:
        """
        Queue broker stop/limit PUT on the async worker (non-blocking tick path).

        ``new_limit`` should be passed when a limit extension has fired — it is used
        directly in the PUT payload, skipping an extra GET to read the current IG limit
        and saving one REST call against the 3-calls/min budget.
        """
        if not self._rest or not deal_id:
            return False
        if deal_id in self._gone_deals:
            return False

        cache_key = f"{deal_id}:{trade_id}"
        last_pushed_stop = self._last_ig_stop.get(cache_key)

        limit_cache_key = f"{deal_id}:{trade_id}:limit"
        last_pushed_limit = self._last_ig_limit.get(limit_cache_key)
        tol = self._stop_tolerance(epic)
        if new_limit is not None and last_pushed_limit is not None:
            if abs(last_pushed_limit - new_limit) < tol:
                new_limit = None

        stop_already_pushed = (
            last_pushed_stop is not None and abs(last_pushed_stop - stop) < tol
        )
        if stop_already_pushed and new_limit is None:
            return False

        if not fast_path:
            ig_stop = self._ig_stop_level(deal_id)
            if ig_stop is not None:
                if side == "BUY" and stop <= ig_stop + tol and new_limit is None:
                    return False
                if side == "SELL" and stop >= ig_stop - tol and new_limit is None:
                    return False

        from execution.stop_dispatch_worker import StopDispatchJob, enqueue_stop_dispatch

        job = StopDispatchJob(
            deal_id=str(deal_id),
            trade_id=int(trade_id),
            side=str(side),
            stop=float(stop),
            epic=str(epic),
            new_limit=float(new_limit) if new_limit is not None else None,
        )
        return enqueue_stop_dispatch(job)

    def _execute_stop_sync(
        self,
        deal_id: str,
        *,
        trade_id: int,
        side: str,
        stop: float,
        epic: str,
        new_limit: float | None = None,
    ) -> bool:
        """Blocking IG REST stop/limit update — runs on stop_dispatch_worker thread."""
        if not self._rest or not deal_id:
            return False
        cache_key = f"{deal_id}:{trade_id}"
        limit_cache_key = f"{deal_id}:{trade_id}:limit"
        try:
            from ig_api.exceptions import IGAPIError, RateLimitError
            from system.rate_limit_manager import get_rate_limit_manager

            get_rate_limit_manager().check_rest_allowed()
            if not hasattr(self._rest, "update_position_stops"):
                return False
            if not self._ig_position_open(deal_id):
                self._close_local_trade_position_gone(
                    trade_id=trade_id,
                    deal_id=deal_id,
                    side=side,
                    epic=epic,
                )
                return False

            if new_limit is not None:
                effective_limit: float | None = new_limit
            else:
                _, effective_limit = self._ig_position_levels(deal_id)

            kwargs: dict[str, float] = {
                "stop_level": self._round_stop_level(stop, epic)
            }
            if effective_limit is not None:
                kwargs["limit_level"] = self._round_stop_level(effective_limit, epic)
            self._rest.update_position_stops(deal_id, **kwargs)
            self._last_ig_stop[cache_key] = stop
            if new_limit is not None:
                self._last_ig_limit[limit_cache_key] = new_limit
            limit_note = (
                f" limit={kwargs['limit_level']:.1f}" if "limit_level" in kwargs else ""
            )
            log_engine(
                f"IG stop updated epic={epic} deal={deal_id} side={side} stop={stop:.1f}{limit_note}"
            )
            return True
        except RateLimitError as e:
            log_engine(f"IG stop update skipped — rate limit: {e}")
        except IGAPIError as e:
            if getattr(e, "status_code", None) == 404:
                self._close_local_trade_position_gone(
                    trade_id=trade_id,
                    deal_id=deal_id,
                    side=side,
                    epic=epic,
                )
            else:
                log_engine(
                    f"IG stop update failed deal={deal_id}: {type(e).__name__}: {e}"
                )
        except Exception as e:
            log_engine(f"IG stop update failed deal={deal_id}: {type(e).__name__}: {e}")
        return False

    @staticmethod
    def _check_exit(side, entry, stop, target, px):
        if side == "BUY":
            if px <= stop:
                hit = (
                    "LOSS"
                    if stop < entry
                    else "BREAKEVEN"
                    if abs(stop - entry) < 1e-9
                    else "WIN"
                )
                return hit, stop
            if px >= target:
                return "WIN", target
        else:
            if px >= stop:
                hit = (
                    "LOSS"
                    if stop > entry
                    else "BREAKEVEN"
                    if abs(stop - entry) < 1e-9
                    else "WIN"
                )
                return hit, stop
            if px <= target:
                return "WIN", target
        return None, None
