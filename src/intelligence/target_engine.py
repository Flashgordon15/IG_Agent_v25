"""
Target-Seeking Alpha Engine — £1,000 daily profit objective on £10k simulated equity.

Tracks daily realised P&L (learning store + IG REST balance session delta),
computes a risk compression factor as profit approaches target, and engages
capital preservation mode once the target is achieved.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")
_VICTORY_LEDGER = Path(__file__).resolve().parents[1] / "data" / "state" / "victory_ledger.jsonl"

DEFAULT_TARGET_DAILY_GBP = 1000.0
DEFAULT_SIMULATED_EQUITY_GBP = 10000.0
MIN_RISK_COMPRESSION_FACTOR = 0.1
CAPITAL_PRESERVATION_ATR_MULT = 1.0
_ENTRY_BLOCK_REASON = "TARGET_ACHIEVED_CAPITAL_PRESERVATION"
# Institutional Capital Harvesting — parabolic milestone snap thresholds
CAPITAL_HARVEST_MILESTONE_PCT = 0.75
CAPITAL_HARVEST_LOCK_FLOOR_PCT = 0.50

# v29.1 night-matrix session lockdown (see intelligence.premium_overnight)
# Legacy weekday blackout 20:00–06:00 BST is DELETED for Gold/Dow/Nikkei/EURUSD.
NIGHT_MATRIX_LOCKDOWN = True

_lock = threading.RLock()
_engine: TargetSeekingEngine | None = None


def risk_compression_factor(p_day: float, target_daily: float) -> float:
    """Factor = max(0.1, 1.0 - (P_day / Target_Daily))."""
    if target_daily <= 0:
        return 1.0
    p = max(0.0, float(p_day))
    return max(MIN_RISK_COMPRESSION_FACTOR, 1.0 - (p / float(target_daily)))


@dataclass
class TargetSeekingEngine:
    """Session-scoped daily profit target tracker."""

    target_daily_gbp: float = DEFAULT_TARGET_DAILY_GBP
    simulated_equity_gbp: float = DEFAULT_SIMULATED_EQUITY_GBP
    enabled: bool = True
    session_start_balance: float | None = None
    last_p_day: float = 0.0
    last_factor: float = 1.0
    capital_preservation: bool = False
    mission_accomplished: bool = False
    last_refresh_ts: float = 0.0
    _session_day: str = ""
    _store: Any | None = field(default=None, repr=False)
    _rest_client: Any | None = field(default=None, repr=False)
    _entry_block_applied: bool = field(default=False, repr=False)

    def configure(self, cfg: Any | None) -> None:
        block = _target_config(cfg)
        self.enabled = bool(block.get("enabled", True))
        self.target_daily_gbp = float(
            block.get("target_daily_gbp", DEFAULT_TARGET_DAILY_GBP)
        )
        self.simulated_equity_gbp = float(
            block.get("simulated_equity_gbp", DEFAULT_SIMULATED_EQUITY_GBP)
        )

    def bind_store(self, store: Any | None) -> None:
        self._store = store

    def bind_rest_client(self, rest_client: Any | None) -> None:
        self._rest_client = rest_client

    def mark_session_start(self, balance: float | None) -> None:
        if balance is None:
            return
        self.session_start_balance = float(balance)
        try:
            from system.drawdown_monitor import reset_session

            reset_session(float(balance), field="balance")
        except Exception:
            pass

    def refresh_balance_from_rest(self) -> float | None:
        rest = self._rest_client
        if rest is None:
            return None
        try:
            if hasattr(rest, "maybe_refresh_account_summary"):
                summary = rest.maybe_refresh_account_summary(min_interval=0.0)
                bal = summary.get("balance")
            elif hasattr(rest, "fetch_account_balance"):
                bal = rest.fetch_account_balance()
            elif hasattr(rest, "get_cached_account_summary"):
                bal = rest.get_cached_account_summary().get("balance")
            else:
                return None
            if bal is None:
                return None
            b = float(bal)
            try:
                from system.drawdown_monitor import update

                update(b, field="balance")
            except Exception:
                pass
            if self.session_start_balance is None:
                self.mark_session_start(b)
            return b
        except Exception:
            return None

    def resolve_open_unrealized_gbp(self) -> float:
        """Sum open broker UPL converted to GBP (multi-currency aware)."""
        total = 0.0
        try:
            from trading.open_position_view import (
                extract_broker_profit_and_loss,
                pnl_currency_amount_to_gbp,
                position_map_from_rows,
            )

            rows: list[dict[str, Any]] = []
            try:
                from api.agent_control import get_trading_loop

                loop_bundle = get_trading_loop()
                if loop_bundle is not None and hasattr(loop_bundle, "loops"):
                    for tl in loop_bundle.loops:
                        sync = getattr(tl, "_position_sync", None)
                        if sync is not None and hasattr(sync, "snapshot_dict"):
                            snap = sync.snapshot_dict()
                            pmap = snap.get("position_map")
                            if isinstance(pmap, dict):
                                rows.extend(pmap.values())
                            else:
                                rows.extend(snap.get("positions") or [])
            except Exception:
                pass
            for pos in position_map_from_rows(rows).values():
                upl, ccy = extract_broker_profit_and_loss(pos)
                if upl is not None:
                    total += pnl_currency_amount_to_gbp(float(upl), ccy)
                elif pos.get("pnl_gbp") is not None:
                    total += float(pos["pnl_gbp"])
        except Exception:
            return 0.0
        return round(total, 2)

    def resolve_p_day_realised(self) -> float:
        """Daily realised profit — learning store primary (signed), session delta fallback.

        For *display/compression* uses max(store, balance) so we do not under-report.
        Capital-preservation engagement uses ``resolve_p_day_for_preservation`` which
        requires broker balance confirmation when REST is bound.
        """
        from decimal import Decimal, ROUND_HALF_UP

        from system.balance_pnl_decimal import decimal_to_float, money_decimal

        two = Decimal("0.01")
        zero = Decimal("0")

        p_store = zero
        if self._store is not None:
            try:
                from system.daily_loss_policy import effective_daily_pnl

                eff = money_decimal(effective_daily_pnl(self._store), field="effective_daily_pnl")
                if eff is not None:
                    p_store = eff
            except Exception:
                p_store = zero

        p_balance = zero
        try:
            from system.drawdown_monitor import snapshot_decimal_debug

            snap = snapshot_decimal_debug()
            if snap.get("last_balance_field_used") == "balance":
                session_pnl = money_decimal(snap.get("session_pnl_decimal"), field="session_pnl")
                if session_pnl is not None:
                    p_balance = session_pnl
        except Exception:
            p_balance = zero

        # Target compression uses realised gains only (non-negative); drawdown guard uses signed path.
        combined = max(p_store, p_balance)
        return decimal_to_float(combined.quantize(two, rounding=ROUND_HALF_UP))

    def resolve_broker_session_pnl_gbp(self) -> float | None:
        """Broker balance session delta — authoritative for capital preservation."""
        from decimal import Decimal

        from system.balance_pnl_decimal import decimal_to_float, money_decimal

        # Prefer live REST balance vs session_start when available.
        if self._rest_client is not None and self.session_start_balance is not None:
            try:
                bal = self.refresh_balance_from_rest()
                if bal is not None:
                    return round(float(bal) - float(self.session_start_balance), 2)
            except Exception:
                pass
        try:
            from system.drawdown_monitor import snapshot_decimal_debug

            snap = snapshot_decimal_debug()
            if snap.get("last_balance_field_used") == "balance":
                session_pnl = money_decimal(snap.get("session_pnl_decimal"), field="session_pnl")
                if session_pnl is not None:
                    return decimal_to_float(session_pnl)
        except Exception:
            pass
        return None

    def resolve_p_day_for_preservation(self) -> float:
        """
        P&L used to engage capital preservation.

        When REST is bound: require broker session balance delta confirmation.
        Inflated learning-store / cascade unrealized alone cannot trip the halt.
        When REST is unbound (unit tests): fall back to realised store path.
        """
        realised = float(self.resolve_p_day_realised() or 0.0)
        if self._rest_client is not None:
            broker_pnl = self.resolve_broker_session_pnl_gbp()
            if broker_pnl is None:
                # No broker confirmation yet — do not halt on journal alone.
                return 0.0
            # Dual-confirm: broker must itself show target hit. Journal may be
            # higher (phantoms/cascade) but cannot engage preservation alone.
            return float(broker_pnl)
        return realised

    def resolve_p_day_total_gbp(self) -> float:
        """Realised + open unrealized P&L in GBP for target/risk compression display."""
        return round(self.resolve_p_day_realised() + self.resolve_open_unrealized_gbp(), 2)

    def _uk_today(self) -> str:
        return datetime.now(tz=_LONDON).date().isoformat()

    def _maybe_reset_uk_midnight(self) -> bool:
        """Reset session tracking at 00:00:00 Europe/London."""
        today = self._uk_today()
        if not self._session_day:
            self._session_day = today
            return False
        if today == self._session_day:
            return False
        prev = self._session_day
        self._session_day = today
        self.last_p_day = 0.0
        self.last_factor = 1.0
        self.capital_preservation = False
        self.mission_accomplished = False
        self._entry_block_applied = False
        self.session_start_balance = None
        try:
            from system.qmm_process_supervisor import clear_process_entry_block

            clear_process_entry_block()
        except Exception:
            pass
        try:
            bal = self.refresh_balance_from_rest()
            if bal is not None:
                self.mark_session_start(bal)
        except Exception:
            pass
        log_engine(
            f"Target engine UK midnight reset: {prev} → {today} — session variables cleared"
        )
        return True

    def _record_victory_ledger(self, p_day: float) -> None:
        payload = {
            "event": "MISSION_ACCOMPLISHED",
            "p_day_gbp": round(p_day, 2),
            "target_daily_gbp": self.target_daily_gbp,
            "simulated_equity_gbp": self.simulated_equity_gbp,
            "uk_date": self._uk_today(),
            "ts_utc": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            "capital_preservation_atr_mult": CAPITAL_PRESERVATION_ATR_MULT,
        }
        try:
            _VICTORY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with _VICTORY_LEDGER.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception as e:
            log_engine(f"Victory ledger write failed: {type(e).__name__}: {e}")
        if self._store is not None:
            try:
                self._store.set_runtime_state(
                    "target_mission_victory",
                    json.dumps(payload, separators=(",", ":")),
                )
            except Exception:
                pass

    def refresh(self, *, force_balance: bool = False) -> dict[str, Any]:
        self._maybe_reset_uk_midnight()
        if force_balance:
            self.refresh_balance_from_rest()
        # Display / compression still track total; preservation uses broker-confirmed path.
        p_day_display = self.resolve_p_day_total_gbp()
        p_day_preserve = self.resolve_p_day_for_preservation()
        factor = risk_compression_factor(max(0.0, p_day_display), self.target_daily_gbp)
        preservation = p_day_preserve >= self.target_daily_gbp

        prev_preservation = self.capital_preservation
        self.last_p_day = p_day_display
        self.last_factor = factor
        self.capital_preservation = preservation
        self.last_refresh_ts = time.time()

        if preservation and not prev_preservation:
            self.mission_accomplished = True
            self._record_victory_ledger(p_day_preserve)
            log_engine(
                f"TARGET ACHIEVED: broker_confirmed P_day=£{p_day_preserve:.2f} "
                f">= £{self.target_daily_gbp:.2f} "
                f"(display=£{p_day_display:.2f}) — Capital Preservation Mode engaged"
            )
        elif preservation:
            self.mission_accomplished = True
        elif prev_preservation and not preservation:
            # Broker no longer confirms target — clear false-positive halt.
            self.mission_accomplished = False
            self._entry_block_applied = False
            try:
                from system.qmm_process_supervisor import clear_process_entry_block

                clear_process_entry_block()
            except Exception:
                pass
            log_engine(
                f"Target engine: capital preservation cleared "
                f"(broker P_day=£{p_day_preserve:.2f} < £{self.target_daily_gbp:.2f})"
            )
        if self.capital_harvest_milestone_snap_active():
            log_engine(
                "CAPITAL_HARVEST [PARABOLIC_SNAP] milestone >= 75% — "
                f"P_day=£{p_day_display:.2f} floor=£{self.capital_harvest_lock_floor_gbp():.2f} engaged"
            )
        self._apply_entry_block_if_needed()
        return self.snapshot()

    def _apply_entry_block_if_needed(self) -> None:
        if not self.capital_preservation:
            return
        if self._entry_block_applied:
            return
        try:
            from system.qmm_process_supervisor import set_process_entry_block

            set_process_entry_block(_ENTRY_BLOCK_REASON)
            self._entry_block_applied = True
            log_engine("Target engine: new entries blocked for session (capital preservation)")
        except Exception as e:
            log_engine(f"Target engine entry block failed: {type(e).__name__}: {e}")

    def risk_compression_factor(self) -> float:
        return float(self.last_factor)

    def capital_preservation_mode(self) -> bool:
        return bool(self.capital_preservation)

    def mission_progress_pct(self) -> float:
        if self.target_daily_gbp <= 0:
            return 0.0
        return min(100.0, max(0.0, (self.last_p_day / self.target_daily_gbp) * 100.0))

    def capital_harvest_milestone_snap_active(self) -> bool:
        """True when broker-reported daily P&L >= 75% of £1,000 target."""
        if self.target_daily_gbp <= 0:
            return False
        return float(self.last_p_day) >= (
            float(self.target_daily_gbp) * CAPITAL_HARVEST_MILESTONE_PCT
        )

    def capital_harvest_lock_floor_gbp(self) -> float:
        """Minimum cash equity floor — 50% of daily milestone (£500 at £1k target)."""
        return round(float(self.target_daily_gbp) * CAPITAL_HARVEST_LOCK_FLOOR_PCT, 2)

    def capital_harvest_contract_snapshot(self) -> dict[str, Any]:
        """Telemetry + trailing engine contract state."""
        snap_active = self.capital_harvest_milestone_snap_active()
        floor = self.capital_harvest_lock_floor_gbp()
        return {
            "milestone_snap_active": snap_active,
            "lock_floor_gbp": floor,
            "milestone_pct": round(CAPITAL_HARVEST_MILESTONE_PCT * 100.0, 1),
            "p_day_gbp": round(self.last_p_day, 2),
            "target_daily_gbp": round(self.target_daily_gbp, 2),
            "policy": "A win is a win",
        }

    def snapshot(self) -> dict[str, Any]:
        open_upl = self.resolve_open_unrealized_gbp()
        realised = self.resolve_p_day_realised()
        return {
            "enabled": self.enabled,
            "p_day_gbp": round(self.last_p_day, 2),
            "p_day_realised_gbp": round(realised, 2),
            "p_day_open_unrealized_gbp": open_upl,
            "target_daily_gbp": round(self.target_daily_gbp, 2),
            "simulated_equity_gbp": round(self.simulated_equity_gbp, 2),
            "risk_compression_factor": round(self.last_factor, 4),
            "mission_progress_pct": round(self.mission_progress_pct(), 1),
            "capital_preservation": self.capital_preservation,
            "capital_preservation_atr_mult": CAPITAL_PRESERVATION_ATR_MULT,
            "mission_accomplished": self.mission_accomplished,
            "session_day_uk": self._session_day or self._uk_today(),
            "session_start_balance": self.session_start_balance,
            "capital_harvest": self.capital_harvest_contract_snapshot(),
        }


def _target_config(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    block = cfg.get("intelligence_layer", {})
    if not isinstance(block, dict):
        return {}
    nested = block.get("target_engine")
    return nested if isinstance(nested, dict) else {}


def target_engine_enabled(cfg: Any | None) -> bool:
    if cfg is None:
        return False
    block = _target_config(cfg)
    if not block:
        return False
    from intelligence.policy import intelligence_enabled

    return intelligence_enabled(cfg) and bool(block.get("enabled", True))


def get_target_engine() -> TargetSeekingEngine:
    global _engine
    with _lock:
        if _engine is None:
            _engine = TargetSeekingEngine()
        return _engine


def initialize_target_engine(
    cfg: Any | None,
    rest_client: Any | None = None,
    *,
    store: Any | None = None,
) -> TargetSeekingEngine:
    """Gate 4 hook — bind store/REST, seed session balance baseline."""
    engine = get_target_engine()
    engine.configure(cfg)
    engine.bind_rest_client(rest_client)
    if store is not None:
        engine.bind_store(store)
    elif cfg is not None:
        try:
            from data.learning_store import LearningStore

            engine.bind_store(LearningStore(str(cfg.learning_db)))
        except Exception:
            pass

    balance: float | None = None
    if rest_client is not None:
        balance = engine.refresh_balance_from_rest()
    if balance is None and cfg is not None:
        try:
            pe = cfg.get("portfolio_envelope") or {}
            if isinstance(pe, dict):
                balance = float(pe.get("account_balance_gbp") or 0) or None
        except (TypeError, ValueError):
            balance = None
    if balance is None:
        balance = DEFAULT_SIMULATED_EQUITY_GBP
    engine.mark_session_start(balance)
    engine._session_day = engine._uk_today()
    snap = engine.refresh()
    log_engine(
        f"Target engine initialized: P_day=£{snap['p_day_gbp']:.2f} "
        f"target=£{snap['target_daily_gbp']:.2f} factor={snap['risk_compression_factor']:.2f}"
    )
    return engine


def apply_target_position_cap(
    cap: int,
    base_cap: int,
    reason: str,
    *,
    cfg: Any | None = None,
) -> tuple[int, str]:
    """Scale ladder/autopilot cap down as daily profit approaches target."""
    if cfg is not None and not target_engine_enabled(cfg):
        return cap, reason
    engine = get_target_engine()
    if not engine.enabled:
        return cap, reason
    engine.refresh()
    if engine.capital_preservation_mode():
        return min(cap, base_cap), f"{reason}; target achieved — preservation cap"
    factor = engine.risk_compression_factor()
    if factor >= 0.999:
        return cap, reason
    excess = max(0, int(cap) - int(base_cap))
    scaled_excess = max(0, int(excess * factor))
    merged = int(base_cap) + scaled_excess
    return merged, f"{reason}; target factor={factor:.2f}"


def apply_target_execution_adjustments(
    execution_params: dict[str, Any],
    *,
    config: Any | None,
) -> tuple[dict[str, Any], str | None]:
    """Merge target risk compression into pre-dispatch params; block if target hit."""
    if not target_engine_enabled(config):
        return execution_params, None

    epic = str(execution_params.get("epic") or execution_params.get("market_epic") or "")
    if epic:
        try:
            from intelligence.premium_overnight import night_matrix_session_allowed

            allowed, session_reason = night_matrix_session_allowed(epic, config=config)
            if not allowed:
                return execution_params, f"NIGHT_MATRIX_SESSION_{session_reason}"
        except Exception:
            pass

    engine = get_target_engine()
    engine.refresh()
    if engine.capital_preservation_mode():
        return execution_params, _ENTRY_BLOCK_REASON
    factor = engine.risk_compression_factor()
    merged = dict(execution_params)
    merged["target_risk_compression_factor"] = factor
    merged["target_p_day_gbp"] = engine.last_p_day
    merged["target_daily_gbp"] = engine.target_daily_gbp
    if factor < 1.0:
        try:
            size = float(merged.get("size") or 0.0)
            if size > 0:
                merged["size"] = max(0.01, size * factor)
                merged["target_size_scaled"] = True
        except (TypeError, ValueError):
            pass
    return merged, None


def reset_target_engine_for_tests() -> None:
    global _engine
    with _lock:
        _engine = None
    try:
        from system.qmm_process_supervisor import clear_process_entry_block

        clear_process_entry_block()
    except Exception:
        pass
