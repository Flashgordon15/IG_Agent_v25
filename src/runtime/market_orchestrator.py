"""
Phase A — run one TradingLoop per enabled instrument; shared PointsEngine and dashboard tick.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import pandas as pd

from api.snapshot import _iso_now
from api.snapshot_store import publish_tick
from system.config import Config
from system.engine_log import log_engine
from trading.trading_loop import TradingLoop

_ORCHESTRATOR_REF: "MarketOrchestrator | None" = None

# Layer 3 — £1k/day global rotation matrix (indices, FX, metals).
# Dashboard + routing monitor all epics; each TradingLoop thread stays epic-scoped.
GLOBAL_ROTATION_UNIVERSE: tuple[str, ...] = (
    # Major global indices
    "IX.D.NIKKEI.IFM.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NASDAQ.IFM.IP",
    "IX.D.DAX.IFM.IP",
    "IX.D.FTSE.IFM.IP",
    "IX.D.CAC.IFM.IP",
    "IX.D.HSI.IFM.IP",
    "IX.D.ASX.IFM.IP",
    "IX.D.SPTRD.IFE.IP",
    # Cross-currency pairs
    "CS.D.EURUSD.CFD.IP",
    "CS.D.GBPUSD.CFD.IP",
    "CS.D.USDJPY.CFD.IP",
    "CS.D.EURGBP.CFD.IP",
    "CS.D.AUDUSD.CFD.IP",
    "CS.D.USDCAD.CFD.IP",
    "CS.D.USDCHF.CFD.IP",
    "CS.D.NZDUSD.CFD.IP",
    "CS.D.EURJPY.CFD.IP",
    "CS.D.GBPJPY.CFD.IP",
    # Precious metals & energy
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.CFPSILVER.CFP.IP",
    "CS.D.CFPPLAT.CFP.IP",
    "CS.D.CRUDE.CFD.IP",
)

TOP_ROTATION_SLOTS = 3
MAX_ROTATION_SLOTS = 5
ROTATION_EXPAND_THRESHOLD_PCT = 10.0
ROTATION_GRACE_CYCLES = 3
ROTATION_MIN_ONLINE_FOR_FILTER = 3
_ROTATION_ONLINE_MAX_AGE_SEC = 30.0
_ROTATION_RANK_FLOOR = 0.01
_FEED_STARVATION_MAX_AGE_SEC = 120.0
_FEED_RECOVERY_MAX_AGE_SEC = 30.0
OFFLINE_BROKER_FEED_REJECTED = "OFFLINE_BROKER_FEED_REJECTED"


def compute_rotation_trend_cleanliness(
    row_15m: pd.Series,
    *,
    atr_15m: float = 0.0,
    atr_5m: float = 0.0,
) -> float:
    """
    Direction-neutral trend strength for rotation rank_score.

    Bull (fast > slow, RSI > 50) and bear (fast < slow, RSI < 50) alignment
    score equally via ``score_trend_factor``. Momentum scales by |EMA gap|/ATR;
    volatility adds a mild boost from absolute ATR (tradable movement, not direction).
    """
    from trading.environment_scorer import score_trend_factor

    alignment = score_trend_factor(row_15m)
    if alignment <= 0:
        return _ROTATION_RANK_FLOOR

    fast = float(row_15m.get("fast_ema", 0))
    slow = float(row_15m.get("slow_ema", 0))
    atr_ref = float(atr_5m or atr_15m or row_15m.get("atr", 0) or 0)
    ema_gap = abs(fast - slow)

    if atr_ref > 0:
        momentum_mult = 0.5 + 0.5 * min(1.0, ema_gap / (2.0 * atr_ref))
        vol_mult = 1.0 + min(0.25, atr_ref / 80.0)
    else:
        momentum_mult = 0.5 + 0.5 * min(1.0, ema_gap / 40.0)
        vol_mult = 1.0

    return max(alignment * momentum_mult * vol_mult, _ROTATION_RANK_FLOOR)


def select_active_rotation_epics(
    ranked_assets: list[tuple[str, float]],
    *,
    base_slots: int = TOP_ROTATION_SLOTS,
    max_slots: int = MAX_ROTATION_SLOTS,
    expand_threshold_pct: float = ROTATION_EXPAND_THRESHOLD_PCT,
    min_online: int = ROTATION_MIN_ONLINE_FOR_FILTER,
) -> list[str]:
    """Build the active rotation window from pre-sorted volatility rank scores.

    Ranking source (same as ``MarketOrchestrator.refresh_active_epics``):
    ``rank_score = trend_cleanliness / relative_spread_cost``, where
    ``trend_cleanliness`` is 15m EMA+RSI alignment (bull or bear), momentum
    from |EMA gap|/ATR, and an ATR volatility boost (see
    ``compute_rotation_trend_cleanliness`` and ``_rotation_rank_score``).

    Default: top ``base_slots`` (3) epics. Expands up to ``max_slots`` (5) when
    4th/5th ranked assets have rank_score within ``expand_threshold_pct`` of the
    3rd-ranked score (score >= third * (1 - pct/100)).
    """
    if len(ranked_assets) < min_online:
        return [epic for epic, _ in ranked_assets]

    third_score = ranked_assets[2][1]
    threshold = third_score * (1.0 - expand_threshold_pct / 100.0)
    slot_count = base_slots
    for i in range(base_slots, min(max_slots, len(ranked_assets))):
        if ranked_assets[i][1] >= threshold:
            slot_count = i + 1
        else:
            break
    return [epic for epic, _ in ranked_assets[:slot_count]]


class MarketOrchestrator:
    """Starts/stops per-epic loops and publishes a merged multi-market tick."""

    def __init__(
        self,
        config: Config,
        loops: list[TradingLoop],
        *,
        primary_epic: str = "",
        enabled_epics: list[str] | None = None,
        instrument_meta: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._config = config
        self._loops = list(loops)
        self._primary_epic = primary_epic or (loops[0]._epic if loops else "")
        loop_epics = [str(loop._epic) for loop in loops if getattr(loop, "_epic", "")]
        passed_enabled = list(enabled_epics or loop_epics)
        universe: list[str] = list(GLOBAL_ROTATION_UNIVERSE)
        for epic in passed_enabled:
            key = str(epic or "").strip()
            if key and key not in universe:
                universe.append(key)
        self._enabled_epics = universe
        self._instrument_meta = dict(instrument_meta or {})
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._running = False
        self._active_epics: list[str] = list(passed_enabled)
        self._active_epics_updated_at: float = 0.0
        self._feed_offline_epics: set[str] = set()

    @property
    def config(self) -> Config:
        return self._config

    @property
    def loops(self) -> list[TradingLoop]:
        return list(self._loops)

    @property
    def primary(self) -> TradingLoop | None:
        if not self._loops:
            return None
        for loop in self._loops:
            if loop._epic == self._primary_epic:
                return loop
        return self._loops[0]

    @property
    def last_context(self) -> Any:
        loop = self.primary
        return loop.last_context if loop is not None else None

    def is_running(self) -> bool:
        return self._running

    def _loop_providing_live_data(self, epic: str, loop: TradingLoop) -> bool:
        """True when epic has a fresh quote or a recent loop snapshot."""
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if (
                snap is not None
                and snap.bid > 0
                and snap.offer > 0
                and snap.age_seconds() <= _ROTATION_ONLINE_MAX_AGE_SEC
            ):
                return True
        except Exception:
            pass
        with self._lock:
            payload = self._snapshots.get(epic)
        if isinstance(payload, dict):
            bid = payload.get("bid")
            offer = payload.get("offer")
            if bid and offer and float(bid) > 0 and float(offer) > 0:
                return True
        return bool(getattr(loop, "_env", None) is not None)

    def _quote_age_seconds(self, epic: str) -> float | None:
        """Last hub quote age in seconds, or None when no live bid/offer."""
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is None or snap.bid <= 0 or snap.offer <= 0:
                return None
            return float(snap.age_seconds())
        except Exception:
            return None

    def _loop_connection_active(self, epic: str, loop: TradingLoop) -> bool:
        """Instrument loop is running with an established (possibly stale) quote path."""
        if not loop.is_running() or loop._env is None:
            return False
        return self._quote_age_seconds(epic) is not None

    def _loop_for_epic(self, epic: str) -> TradingLoop | None:
        key = str(epic or "").strip()
        if not key:
            return None
        for loop in self._loops:
            if str(getattr(loop, "_epic", "") or "") == key:
                return loop
        return None

    def _apply_feed_circuit_breakers(self) -> set[str]:
        """
        Autonomous feed-stale interceptor — isolate starving epics in RAM only.

        Never stops sibling loops; only flags entry gates and ejects from rotation pool.
        """
        offline_now: set[str] = set()
        for loop in self._loops:
            epic = str(getattr(loop, "_epic", "") or "")
            if not epic:
                continue
            age = self._quote_age_seconds(epic)
            if not self._loop_connection_active(epic, loop):
                if epic in self._feed_offline_epics:
                    loop.clear_entry_circuit_breaker()
                continue
            if age is not None and age > _FEED_STARVATION_MAX_AGE_SEC:
                offline_now.add(epic)
                if epic not in self._feed_offline_epics:
                    log_engine(
                        f"CIRCUIT_BREAKER_ACTIVE | epic={epic} quote_age={age:.0f}s "
                        f"(>{_FEED_STARVATION_MAX_AGE_SEC:.0f}) — {OFFLINE_BROKER_FEED_REJECTED}"
                    )
                loop.set_entry_circuit_breaker(OFFLINE_BROKER_FEED_REJECTED)
            elif epic in self._feed_offline_epics and age is not None:
                if age <= _FEED_RECOVERY_MAX_AGE_SEC:
                    loop.clear_entry_circuit_breaker()
                    log_engine(
                        f"CIRCUIT_BREAKER_CLEARED | epic={epic} quote_age={age:.1f}s — feed restored"
                    )
                else:
                    offline_now.add(epic)
                    loop.set_entry_circuit_breaker(OFFLINE_BROKER_FEED_REJECTED)

        self._feed_offline_epics = offline_now
        return set(offline_now)

    def get_feed_offline_epics(self) -> list[str]:
        with self._lock:
            return sorted(self._feed_offline_epics)

    def _relative_spread_cost(self, epic: str, loop: TradingLoop) -> float:
        """Broker spread deviation — prefer env-scorer factor, else live quote ratio."""
        env = loop._env
        if env is not None and hasattr(env, "get_factors"):
            try:
                from trading.environment_scorer import FACTOR_SPREAD_MAX

                factors = env.get_factors()
                spread_factor = float(factors.get("spread") or 0.0)
                if spread_factor > 0:
                    return max(
                        FACTOR_SPREAD_MAX / spread_factor,
                        _ROTATION_RANK_FLOOR,
                    )
            except Exception:
                pass

        current_spread: float | None = None
        normal_spread: float | None = None
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is not None and snap.bid > 0 and snap.offer > 0:
                current_spread = float(snap.offer) - float(snap.bid)
        except Exception:
            pass
        if current_spread is None:
            with self._lock:
                payload = self._snapshots.get(epic) or {}
            bid = payload.get("bid")
            offer = payload.get("offer")
            if bid is not None and offer is not None:
                try:
                    current_spread = float(offer) - float(bid)
                except (TypeError, ValueError):
                    current_spread = None
        if normal_spread is None or normal_spread <= 0:
            meta = self._instrument_meta.get(epic, {})
            cfg_pts = meta.get("max_spread_pts")
            if cfg_pts is not None:
                try:
                    normal_spread = float(cfg_pts)
                except (TypeError, ValueError):
                    normal_spread = None
            if normal_spread is None or normal_spread <= 0:
                normal_spread = max(
                    current_spread or _ROTATION_RANK_FLOOR, _ROTATION_RANK_FLOOR
                )
        if current_spread is None or current_spread <= 0:
            return max(normal_spread, _ROTATION_RANK_FLOOR)
        return max(
            current_spread / max(normal_spread, _ROTATION_RANK_FLOOR),
            _ROTATION_RANK_FLOOR,
        )

    def _trend_cleanliness(self, loop: TradingLoop) -> float:
        """15m EMA+RSI alignment (bull or bear) × |EMA gap| momentum × ATR vol."""
        engine = getattr(loop, "_signal_engine", None)
        market = str(getattr(loop, "_market", "") or "")
        if engine is not None and market:
            try:
                df = engine.quote_df(market)
                c15 = engine.candles(df, 15)
                if len(c15) >= 2:
                    c15i = engine.add_indicators(c15)
                    row15 = c15i.iloc[-2]
                    atr_15 = float(row15.get("atr", 0) or 0)
                    atr_5 = 0.0
                    c5 = engine.candles(df, 5)
                    if len(c5) >= 2:
                        c5i = engine.add_indicators(c5)
                        atr_5 = float(c5i.iloc[-2].get("atr", 0) or 0)
                    return compute_rotation_trend_cleanliness(
                        row15,
                        atr_15m=atr_15,
                        atr_5m=atr_5,
                    )
            except Exception:
                pass

        env = loop._env
        if env is None:
            return _ROTATION_RANK_FLOOR
        try:
            if hasattr(env, "get_factors"):
                factors = env.get_factors()
                trend = float(factors.get("trend") or 0.0)
                if trend > 0:
                    atr_pts = float(factors.get("atr") or 0.0)
                    vol_mult = 1.0 + min(0.25, atr_pts / 80.0) if atr_pts > 0 else 1.0
                    return max(trend * vol_mult, _ROTATION_RANK_FLOOR)
            last = getattr(env, "_last", None)
            fitness = float(getattr(last, "total", 0.0) or 0.0)
            if fitness > 0:
                return max(fitness * 0.25, _ROTATION_RANK_FLOOR)
        except Exception:
            pass
        return _ROTATION_RANK_FLOOR

    def _rotation_rank_score(self, epic: str, loop: TradingLoop) -> float:
        trend_cleanliness = self._trend_cleanliness(loop)
        relative_spread_cost = self._relative_spread_cost(epic, loop)
        return trend_cleanliness / relative_spread_cost

    def _strategy_session_eligible(self, epic: str) -> bool:
        """True when epic's instrument whitelist includes the current strategy session."""
        try:
            from signals.indicators import session_name
            from trading.instrument_registry import InstrumentRegistry

            wl = InstrumentRegistry(self._config.as_dict()).session_whitelist_for_epic(
                epic
            )
            if not wl:
                wl = list(getattr(self._config, "trading_session_whitelist", []) or [])
            if not wl:
                return True
            return session_name() in wl
        except Exception:
            return True

    def refresh_active_epics(self) -> list[str]:
        """Layer 3 Hot Market Selector — rank_score = trend_cleanliness / relative_spread_cost.

        trend_cleanliness uses absolute 15m trend alignment (bull or bear) and
        momentum/volatility scaling, not upward direction alone.
        """
        import time

        feed_offline = self._apply_feed_circuit_breakers()

        ranked_assets: list[tuple[str, float]] = []
        for loop in self._loops:
            epic = str(getattr(loop, "_epic", "") or "")
            if not epic or loop._env is None:
                continue
            if epic in feed_offline:
                continue
            if not self._loop_providing_live_data(epic, loop):
                continue
            if not self._strategy_session_eligible(epic):
                continue
            try:
                rank_score = self._rotation_rank_score(epic, loop)
            except Exception:
                continue
            ranked_assets.append((epic, rank_score))

        ranked_assets.sort(key=lambda item: item[1], reverse=True)

        cfg = self._config.as_dict() if hasattr(self._config, "as_dict") else {}
        base_slots = int(cfg.get("rotation_base_slots") or TOP_ROTATION_SLOTS)
        max_slots = int(cfg.get("rotation_max_slots") or MAX_ROTATION_SLOTS)
        expand_pct = float(
            cfg.get("rotation_expand_threshold_pct") or ROTATION_EXPAND_THRESHOLD_PCT
        )
        if len(ranked_assets) < ROTATION_MIN_ONLINE_FOR_FILTER:
            active = [epic for epic, _ in ranked_assets]
        else:
            active = select_active_rotation_epics(
                ranked_assets,
                base_slots=base_slots,
                max_slots=max_slots,
                expand_threshold_pct=expand_pct,
            )

        with self._lock:
            self._active_epics = active
            self._active_epics_updated_at = time.time()
        return list(self._active_epics)

    def get_active_epics(self) -> list[str]:
        with self._lock:
            return list(self._active_epics)

    @staticmethod
    def get_global_active_epics() -> list[str]:
        global _ORCHESTRATOR_REF
        if _ORCHESTRATOR_REF is None:
            return []
        return _ORCHESTRATOR_REF.get_active_epics()

    @staticmethod
    def get_signal_engine_for_market(market: str) -> Any | None:
        """Resolve the per-market SignalEngine from a running orchestrator loop."""
        global _ORCHESTRATOR_REF
        key = str(market or "").strip()
        if not key or _ORCHESTRATOR_REF is None:
            return None
        for loop in _ORCHESTRATOR_REF._loops:
            if str(getattr(loop, "_market", "") or "") == key:
                return getattr(loop, "_signal_engine", None)
        return None

    @staticmethod
    def hot_reload_config(config: Config | None = None) -> int:
        """Push reloaded Config into orchestrator + all trading loops (in-memory)."""
        global _ORCHESTRATOR_REF
        if _ORCHESTRATOR_REF is None:
            return 0
        from system.config_loader import get_config

        cfg = config or get_config(reload=True)
        _ORCHESTRATOR_REF._config = cfg
        for loop in _ORCHESTRATOR_REF._loops:
            loop._config = cfg
        return len(_ORCHESTRATOR_REF._loops)

    def start(self) -> None:
        if self._running:
            return
        try:
            from system.market_data_hub import get_market_data_hub
            from system.stream_ready import is_stream_ready, signal_stream_ready

            if not is_stream_ready():
                hub = get_market_data_hub()
                for epic in self._enabled_epics:
                    snap = hub.get_snapshot(epic)
                    if (
                        snap is not None
                        and snap.bid > 0
                        and snap.offer > 0
                        and snap.age_seconds() <= 30.0
                    ):
                        signal_stream_ready(source=f"orchestrator_start:{epic}")
                        break
        except Exception as e:
            log_engine(
                f"market_orchestrator stream_ready preflight failed: "
                f"{type(e).__name__}: {e}"
            )
        self._stop.clear()
        self._running = True
        for loop in self._loops:
            loop.start()
        log_engine(
            f"market_orchestrator started ({len(self._loops)} loops) "
            f"primary={self._primary_epic}"
        )
        self._health_monitor_thread = threading.Thread(
            target=self._loop_health_monitor,
            name="ig-orchestrator-health",
            daemon=True,
        )
        self._health_monitor_thread.start()

    def _loop_health_monitor(self) -> None:
        """Detect and respawn individual trading loops that stopped due to deadlock."""
        import time

        check_interval = 20.0
        respawn_cooldown: dict[str, float] = {}
        zombie_alert_sent = False

        while not self._stop.wait(check_interval):
            if not self._running:
                break
            any_running = any(loop.is_running() for loop in self._loops)
            if self._running and self._loops and not any_running:
                if not zombie_alert_sent:
                    zombie_alert_sent = True
                    log_engine(
                        "CRITICAL: all trading loops stopped while orchestrator running"
                    )
                    try:
                        from system.telegram_notifier import send_critical_alert

                        send_critical_alert(
                            "⚠️ Trading loops STOPPED — no trades firing"
                        )
                    except Exception as e:
                        log_engine(
                            f"telegram zombie-loop alert failed: {type(e).__name__}: {e}"
                        )
            else:
                zombie_alert_sent = False
            for loop in self._loops:
                if self._stop.is_set():
                    break
                if loop.is_running():
                    continue
                epic = getattr(loop, "_epic", "?")
                market = getattr(loop, "_market", epic)
                last_respawn = respawn_cooldown.get(epic, 0.0)
                if time.monotonic() - last_respawn < 30.0:
                    continue
                respawn_cooldown[epic] = time.monotonic()
                log_engine(
                    f"Orchestrator health monitor: respawning stopped loop "
                    f"market={market} epic={epic}"
                )
                try:
                    from system.telegram_notifier import get_telegram_notifier

                    notifier = get_telegram_notifier()
                    if notifier is not None:
                        notifier.send_alert(
                            f"🔄 Auto-respawning {market} loop after deadlock",
                            dedupe_key=f"respawn:{epic}",
                        )
                except Exception:
                    pass
                try:
                    loop.start()
                except Exception as e:
                    log_engine(f"Orchestrator respawn failed for {epic}: {e}")

    def stop(self) -> None:
        self._stop.set()
        for loop in self._loops:
            loop.stop()
        self._running = False
        log_engine("market_orchestrator stopped")

    def run_once(self) -> None:
        """Run one tick on each loop (tests)."""
        for loop in self._loops:
            loop.run_once()
        self._publish_merged()

    def on_market_snapshot(self, payload: dict[str, Any]) -> None:
        epic = str(payload.get("epic") or "").strip()
        if not epic:
            return
        with self._lock:
            self._snapshots[epic] = payload
        self.refresh_active_epics()
        self._publish_merged()

    def _placeholder_market_slice(self, epic: str) -> dict[str, Any]:
        """Minimal per-market tick when a loop has not published yet (tab stays visible)."""
        meta = self._instrument_meta.get(epic, {})
        label = str(meta.get("name") or epic)
        instrument_id = str(meta.get("instrument_id") or "")
        bid: float | None = None
        offer: float | None = None
        tick_age_s: float | None = None
        stream_status = "DISCONNECTED"
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is not None and snap.bid > 0 and snap.offer > 0:
                bid = float(snap.bid)
                offer = float(snap.offer)
                tick_age_s = float(snap.age_seconds())
                stream_status = "LIVE"
        except Exception:
            pass
        spread = round(float(offer) - float(bid), 5) if bid and offer else None
        return {
            "type": "tick",
            "epic": epic,
            "market": label,
            "instrument_id": instrument_id,
            "ts": _iso_now(),
            "market_state": "OPEN" if bid and offer else "OFFLINE",
            "bid": bid,
            "offer": offer,
            "spread": spread,
            "tick_age_s": tick_age_s,
            "stream_status": stream_status,
            "health": {
                "badge": "WATCHING",
                "badge_text": "Awaiting loop snapshot",
                "gates": [],
                "summary": "Loop snapshot pending — stream may still be live",
            },
            "signal": {
                "direction": "WAIT",
                "confidence": 0,
                "fitness": 0,
                "setup": "",
            },
            "positions": [],
        }

    def _markets_for_dashboard(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            markets = {k: dict(v) for k, v in self._snapshots.items()}
        for epic in self._enabled_epics:
            if epic and epic not in markets:
                markets[epic] = self._placeholder_market_slice(epic)
        return markets

    def _publish_merged(self) -> None:
        markets = self._markets_for_dashboard()
        if not markets:
            return
        from trading.open_position_view import epic_market_label

        primary = markets.get(self._primary_epic) or next(iter(markets.values()))
        merged = dict(primary)
        merged["markets"] = markets
        enabled = list(self._enabled_epics or markets.keys())
        merged["enabled_epics"] = enabled
        merged["instrument_labels"] = {
            epic: epic_market_label(epic) for epic in enabled
        }
        # Union epic-scoped closed trades from each slice (dedupe by deal_id).
        closed_union: list[dict[str, Any]] = []
        seen_closed: set[str] = set()
        for epic_key in enabled:
            mslice = markets.get(epic_key) or {}
            for row in mslice.get("closed_trades") or []:
                if not isinstance(row, dict):
                    continue
                deal_key = str(
                    row.get("deal_id")
                    or row.get("ig_deal_id")
                    or f"{row.get('epic')}-{row.get('closed_at')}"
                )
                if deal_key in seen_closed:
                    continue
                seen_closed.add(deal_key)
                closed_union.append(row)
        closed_union.sort(
            key=lambda r: str(r.get("closed_at") or r.get("time") or ""),
            reverse=True,
        )
        merged["closed_trades"] = closed_union[:100]
        merged["selected_epic"] = self._primary_epic
        merged["orchestrator"] = {
            "loop_count": len(self._loops),
            "primary_epic": self._primary_epic,
            "active_epics": self.get_active_epics(),
            "feed_offline_epics": self.get_feed_offline_epics(),
        }
        try:
            from system.gate_relaxation import relaxation_snapshot

            merged["gate_relaxations"] = relaxation_snapshot()
        except Exception:
            pass
        try:
            publish_tick(merged)
        except Exception as e:
            log_engine(f"publish_tick merged failed: {type(e).__name__}: {e}")


def attach_snapshot_handlers(orchestrator: MarketOrchestrator) -> None:
    """Wire each loop to feed the orchestrator merge publisher."""
    global _ORCHESTRATOR_REF
    _ORCHESTRATOR_REF = orchestrator
    handler: Callable[[dict[str, Any]], None] = orchestrator.on_market_snapshot
    for loop in orchestrator.loops:
        loop._on_snapshot = handler
        loop._publish_snapshots = False
    orchestrator.refresh_active_epics()
