"""
V6.2 Autonomic Disaster Recovery — volatile ledger checkpoints + broker re-sync.

``V2DisasterRecoveryManager`` checkpoints in-memory position state to
``trading_ledger.json`` on every transaction mutation, then reconciles broker
truth via GET ``/gateway/deal/v1/positions/otc`` before trading loops build.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir

_LEDGER_PATH = data_dir() / "state" / "trading_ledger.json"
_CHECKPOINT_THREAD = "ig-v6.2-disaster-recovery"
_CHECKPOINT_POLL_SEC = 0.15
_BROKER_FETCH_TIMEOUT_SEC = 8.0
_POSITION_OTC_PATH = "/v1/positions/otc"

_V62_PROBE_EPICS: tuple[str, ...] = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.CRUDE.CFD.IP",
    "IX.D.FTSE.IFM.IP",
    "IX.D.DAX.IFM.IP",
)

_lock = threading.Lock()
_manager_ref: V2DisasterRecoveryManager | None = None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_blob(data: dict[str, Any]) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class V2DisasterRecoveryManager:
    """Thread-safe disaster recovery coordinator."""

    def __init__(self) -> None:
        self._checkpoint_lock = threading.Lock()
        self._monitor_started = False
        self._monitor_thread: threading.Thread | None = None
        self._last_fingerprint: str | None = None
        self._last_checkpoint_hash: str | None = None
        self._recovery_stats: dict[str, Any] = {
            "checkpoints_written": 0,
            "broker_resyncs": 0,
            "positions_rebuilt": 0,
            "last_checkpoint_at": None,
            "last_resync_at": None,
        }

    @property
    def stats(self) -> dict[str, Any]:
        with self._checkpoint_lock:
            return dict(self._recovery_stats)

    def start_checkpoint_monitor(self) -> None:
        """Background monitor — cryptographic ledger flush on position mutations."""
        if os.environ.get("IG_AGENT_PYTEST") == "1":
            return
        with self._checkpoint_lock:
            if self._monitor_started:
                return
            self._monitor_thread = threading.Thread(
                target=self._checkpoint_monitor_loop,
                name=_CHECKPOINT_THREAD,
                daemon=True,
            )
            self._monitor_thread.start()
            self._monitor_started = True
        log_engine("V6.2 disaster recovery: checkpoint monitor armed")

    def notify_transaction_state_change(self, *, reason: str = "") -> None:
        """Immediate checkpoint on order open / stop modify / close."""
        self.flush_cryptographic_checkpoint(reason=reason or "transaction_mutation")

    def flush_cryptographic_checkpoint(self, *, reason: str = "") -> bool:
        """Atomic ledger write with SHA-256 checkpoint envelope."""
        try:
            from system.shutdown_cleanup import (
                _build_trading_ledger_snapshot,
                _position_state_fingerprint,
                force_flush_trading_ledger_checkpoint,
            )
        except ImportError:
            return False

        fp = _position_state_fingerprint()
        with self._checkpoint_lock:
            if fp == self._last_fingerprint and self._last_checkpoint_hash:
                return False

        try:
            snapshot = _build_trading_ledger_snapshot()
            checkpoint_hash = _sha256_blob(snapshot)
            snapshot["checkpoint_sha256"] = checkpoint_hash
            snapshot["checkpoint_reason"] = reason or "v6.2_recovery_mgr"
            snapshot["checkpoint_at"] = _utc_iso()
            written = force_flush_trading_ledger_checkpoint(snapshot)
            if written:
                with self._checkpoint_lock:
                    self._last_fingerprint = fp
                    self._last_checkpoint_hash = checkpoint_hash
                    self._recovery_stats["checkpoints_written"] += 1
                    self._recovery_stats["last_checkpoint_at"] = snapshot["checkpoint_at"]
                log_engine(
                    f"V6.2 checkpoint: trading_ledger.json "
                    f"sha256={checkpoint_hash[:12]}… reason={reason or 'mutation'}"
                )
            return written
        except Exception as exc:
            log_guarded_exception("v6.2_checkpoint_flush", exc)
            return False

    def _checkpoint_monitor_loop(self) -> None:
        while True:
            try:
                from system.shutdown_cleanup import _position_state_fingerprint

                fp = _position_state_fingerprint()
                with self._checkpoint_lock:
                    changed = fp != self._last_fingerprint
                if changed:
                    self.flush_cryptographic_checkpoint(reason="monitor_poll")
            except Exception as exc:
                log_guarded_exception("v6.2_checkpoint_monitor", exc)
            time.sleep(_CHECKPOINT_POLL_SEC)

    def fetch_broker_positions_async(
        self,
        rest_client: Any,
        *,
        timeout_sec: float = _BROKER_FETCH_TIMEOUT_SEC,
    ) -> list[dict[str, Any]]:
        """HTTPS GET ``/gateway/deal/v1/positions/otc`` on a worker thread."""

        def _fetch() -> list[dict[str, Any]]:
            rest_client.ensure_session()
            response = rest_client.request(
                "GET",
                _POSITION_OTC_PATH,
                headers=rest_client._auth_headers("2"),
            )
            if response.status_code == 401:
                rest_client.login()
                response = rest_client.request(
                    "GET",
                    _POSITION_OTC_PATH,
                    headers=rest_client._auth_headers("2"),
                )
            if response.status_code != 200:
                return list(rest_client.open_positions() or [])
            payload = response.json()
            rows = payload.get("positions")
            if isinstance(rows, list):
                return rows
            return list(rest_client.open_positions() or [])

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v62-broker")
        fut = pool.submit(_fetch)
        try:
            return list(fut.result(timeout=timeout_sec))
        except FuturesTimeoutError:
            log_engine("V6.2 broker resync: positions/otc timed out — fallback empty")
            return []
        finally:
            # Never block gate worker on shutdown — timed-out REST may still be in flight.
            pool.shutdown(wait=False, cancel_futures=True)

    def reconcile_failsafe_cache(
        self,
        broker_positions: list[dict[str, Any]],
        *,
        config: Any | None = None,
    ) -> int:
        """
        Rebuild volatile RAM + learning rows for broker positions missing locally.
        Applies iron-clad 10pt stop / compound floor sizing boundaries.
        """
        if not broker_positions:
            return 0

        from execution.epic_normalizer import normalize_night_matrix_epic
        from execution.types import force_inject_gate_execution_params
        from harmonization.iron_clad_risk import (
            MANDATORY_LIMIT_POINTS,
            MANDATORY_STOP_POINTS,
        )

        local_deals: set[str] = set()
        try:
            from system.config_loader import get_config
            from data.learning_store import LearningStore

            cfg = config or get_config()
            store = LearningStore(str(cfg.learning_db))
            for row in store.active_trades() or []:
                deal = str(row.get("ig_deal_id") or row.get("deal_id") or "").strip()
                if deal:
                    local_deals.add(deal)
        except Exception as exc:
            log_guarded_exception("v6.2_local_deals", exc)
            store = None

        rebuilt = 0
        volatile_positions: list[dict[str, Any]] = []

        for item in broker_positions:
            if not isinstance(item, dict):
                continue
            market = item.get("market") or {}
            position = item.get("position") or {}
            deal_id = str(position.get("dealId") or position.get("deal_id") or "").strip()
            if not deal_id or deal_id in local_deals:
                continue
            epic = normalize_night_matrix_epic(str(market.get("epic") or ""))
            if not epic:
                continue
            direction = str(position.get("direction") or "BUY").upper()
            try:
                size = float(position.get("size") or 0)
                level = float(position.get("level") or market.get("bid") or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0 or level <= 0:
                continue

            gate = force_inject_gate_execution_params(
                epic=epic,
                size=min(size, 1.0),
                stop_points=MANDATORY_STOP_POINTS,
                limit_points=MANDATORY_LIMIT_POINTS,
            )
            stop_level = float(position.get("stopLevel") or 0)
            limit_level = float(position.get("limitLevel") or 0)

            if store is not None:
                try:
                    store.import_ig_position(
                        epic=epic,
                        market=str(market.get("instrumentName") or epic),
                        side=direction,
                        entry=level,
                        size=float(gate.get("size") or gate.get("actual_size") or 1.0),
                        deal_id=deal_id,
                        deal_reference=str(position.get("dealReference") or ""),
                        notes="v6.2_disaster_recovery",
                        stop_level=stop_level,
                        limit_level=limit_level,
                    )
                    rebuilt += 1
                    local_deals.add(deal_id)
                except Exception as exc:
                    log_guarded_exception("v6.2_import_position", exc)

            volatile_positions.append(
                {
                    "deal_id": deal_id,
                    "epic": epic,
                    "direction": direction,
                    "size": gate.get("size"),
                    "stop_points": gate.get("stop_points"),
                    "limit_points": gate.get("limit_points"),
                    "entry": level,
                    "gate_execution_params": gate,
                }
            )

        if volatile_positions:
            try:
                from trading.cache_reaper import (
                    volatile_fulfillment_cache_merge,
                    volatile_runtime_state_merge,
                )

                volatile_runtime_state_merge(
                    {
                        "disaster_recovery": True,
                        "open_positions": volatile_positions,
                        "accepting_ticks": True,
                        "recovered_at": _utc_iso(),
                    }
                )
                volatile_fulfillment_cache_merge(
                    {"by_epic": {row["epic"]: row for row in volatile_positions}}
                )
            except Exception as exc:
                log_guarded_exception("v6.2_volatile_rebuild", exc)

            self.notify_transaction_state_change(reason="failsafe_reconcile")
            with self._checkpoint_lock:
                self._recovery_stats["positions_rebuilt"] += rebuilt
            log_engine(
                f"V6.2 failsafe reconcile: rebuilt {rebuilt} broker position(s) in RAM"
            )

        return rebuilt

    def run_post_crash_handshake(
        self,
        *,
        rest_client: Any,
        config: Any | None = None,
        system_state: Any | None = None,
        boot_context: Any | None = None,
    ) -> dict[str, Any]:
        """
        Pre-loop broker truth fetch + failsafe reconciliation.
        Returns summary dict stored on ``BootContext.hydration_detail``.
        """
        summary: dict[str, Any] = {
            "broker_positions": 0,
            "rebuilt": 0,
            "fast_ready": False,
            "ledger_restored": False,
        }

        if _LEDGER_PATH.is_file():
            try:
                from trading.cache_reaper import hydrate_volatile_caches_from_disk

                hydrate_volatile_caches_from_disk()
                summary["ledger_restored"] = True
            except Exception as exc:
                log_guarded_exception("v6.2_ledger_hydrate", exc)

        if rest_client is None:
            return summary

        try:
            broker_rows = self.fetch_broker_positions_async(rest_client)
            summary["broker_positions"] = len(broker_rows)
            with self._checkpoint_lock:
                self._recovery_stats["broker_resyncs"] += 1
                self._recovery_stats["last_resync_at"] = _utc_iso()
            log_engine(
                f"V6.2 broker resync: GET {_POSITION_OTC_PATH} "
                f"→ {len(broker_rows)} open position(s)"
            )
            rebuilt = self.reconcile_failsafe_cache(broker_rows, config=config)
            summary["rebuilt"] = rebuilt
            summary["fast_ready"] = rebuilt > 0
        except Exception as exc:
            log_guarded_exception("v6.2_post_crash_handshake", exc)

        if summary["fast_ready"] and system_state is not None:
            try:
                from system.system_state import BootPhase

                snap = system_state.snapshot_model()
                total = int(snap.loops.built or 7)
                system_state.update_state(
                    BootPhase.G5,
                    100,
                    "ACTIVE — disaster recovery",
                    hydration={
                        "positions_synced": True,
                        "orders_synced": True,
                        "ohlc_epics_ready": max(int(snap.hydration.ohlc_epics_ready or 0), total),
                        "ohlc_epics_total": max(total, 7),
                    },
                    loops={
                        "built": max(total, 7),
                        "running": True,
                        "accepting_ticks": True,
                    },
                    ready=True,
                )
                log_engine("V6.2 disaster recovery: advanced boot to G5 READY (reconciled)")
            except Exception as exc:
                log_guarded_exception("v6.2_advance_g5", exc)

        if boot_context is not None:
            try:
                boot_context.hydration_detail["v62_recovery"] = summary
            except Exception:
                pass

        return summary


def get_disaster_recovery_manager() -> V2DisasterRecoveryManager:
    global _manager_ref
    with _lock:
        if _manager_ref is None:
            _manager_ref = V2DisasterRecoveryManager()
        return _manager_ref


def run_v62_pre_loop_disaster_recovery(
    *,
    rest_client: Any,
    config: Any | None = None,
    system_state: Any | None = None,
    boot_context: Any | None = None,
) -> dict[str, Any]:
    """Gate 4 entry — broker handshake before orchestrator build."""
    mgr = get_disaster_recovery_manager()
    return mgr.run_post_crash_handshake(
        rest_client=rest_client,
        config=config,
        system_state=system_state,
        boot_context=boot_context,
    )


def start_v62_disaster_recovery_services() -> None:
    """Arm continuous checkpoint monitor (post-G5)."""
    get_disaster_recovery_manager().start_checkpoint_monitor()


def reset_disaster_recovery_for_tests() -> None:
    global _manager_ref
    with _lock:
        _manager_ref = None


def v62_probe_epics() -> tuple[str, ...]:
    return _V62_PROBE_EPICS
