"""
Trade Support Wrapper — always-on open-TRADE supervisor (broker = source of truth).

Distinct from ``runtime.desk_support_wrapper`` (which watches the *agent process*
— health, locks, restarts). This wrapper watches every *open trade* at the broker
and guarantees each one is actively managed, regardless of whether the in-process
``OpenPositionManager`` is alive, ticking, or reporting stale data.

Why this exists
---------------
The in-process manager and the ``/api/positions/live`` cache can go stale or die
(``tick_count: 0``, ``pnl_gbp: None``, wrong sizes/counts). When that happens the
desk reports ``unmonitored: 0`` while trades actually bleed unmanaged. This wrapper
never trusts the in-process cache: every cycle it pulls the authoritative open book
straight from IG REST (``budget_priority``), values each trade in GBP, arms full risk
coverage (GBP exit + virtual stop + dynamic trail), and executes exits for:

* per-trade risk breaches (soft-loss, loss-cap, trail-floor, target, quick-win,
  stagnant dead-zone) — via ``assess_open_positions``
* **redundant** trades over the per-epic / global cap — via ``_cap_breach_actions``
* **stale** trades past ``max_position_age_minutes`` — via ``_age_breach_actions``
* **unmanageable** trades that cannot be valued for N consecutive cycles AND are
  aged — force-flattened so nothing bleeds silently behind a broken quote feed.

Launch::

    IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
      PYTHONPATH=src python3 -m runtime.trade_support_wrapper

Or via ``scripts/trade_support_wrapper.sh`` (launchd-friendly).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "poll_interval_sec": 12.0,
    # A trade with no computable P&L for this many consecutive cycles is treated
    # as unmanageable; if it is also older than unmanageable_min_age_min it is
    # force-flattened so it cannot bleed behind a broken quote feed.
    "unmanageable_cycles": 5,
    "unmanageable_min_age_min": 10.0,
    # Force-flatten unmanageable aged trades. Off => flag only (audit + status).
    "flatten_unmanageable": True,
    # Arm full risk stack every cycle when true (config alias: arm_stack_every_cycle).
    "arm_stack_every_cycle": False,
    # The heavy full risk-stack reconcile runs every Nth cycle unless
    # arm_stack_every_cycle is true. 0/1 => every cycle.
    "arm_every_n_cycles": 5,
    "max_actions_per_cycle": 8,
    # Reuse another process's broker snapshot when it is this fresh (shared
    # REST budget — avoids every process polling /positions independently).
    "snapshot_coordinate_age_sec": 6.0,
}

_stop = False


def _audit_path() -> Any:
    return data_dir() / "trade_support_audit.jsonl"


def _status_path() -> Any:
    return data_dir() / "trade_support_status.json"


def _load_cfg_block(cfg: Any | None = None) -> dict[str, Any]:
    out = dict(_DEFAULTS)
    block: Any = {}
    if cfg is not None:
        block = (
            cfg.get("trade_support_wrapper")
            if hasattr(cfg, "get")
            else getattr(cfg, "trade_support_wrapper", None)
        ) or {}
    if isinstance(block, dict):
        for key, val in block.items():
            if not str(key).startswith("_"):
                out[key] = val
    return out


def _audit(event: str, detail: dict[str, Any]) -> None:
    row = {"ts": time.time(), "event": event, **detail}
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _write_status(status: dict[str, Any]) -> None:
    try:
        path = _status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(status, default=str, indent=2)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        # Mirror for pre-deploy main that still resolves legacy src/data.
        try:
            from system.paths import legacy_src_data_dir

            legacy = legacy_src_data_dir() / "trade_support_status.json"
            if legacy.resolve() != path.resolve():
                legacy.write_text(body, encoding="utf-8")
        except Exception:
            pass
    except OSError:
        pass


def open_mins_from_item(item: dict[str, Any]) -> float | None:
    """Minutes since entry from an IG open-position REST item's created date."""
    pos = item.get("position") or {}
    raw = str(
        pos.get("createdDateUTC")
        or pos.get("createdDate")
        or pos.get("created")
        or ""
    ).strip()
    if not raw:
        return None
    for fmt in ("iso", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M:%S:%f"):
        try:
            if fmt == "iso":
                opened = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                opened = datetime.strptime(raw, fmt)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return max(0.0, (now - opened).total_seconds() / 60.0)
        except (ValueError, TypeError):
            continue
    return None


@dataclass
class TradeSupportState:
    started_mono: float = field(default_factory=time.monotonic)
    cycles: int = 0
    last_cycle_at: float = 0.0
    last_error: str = ""
    # deal_id -> consecutive cycles with no computable P&L
    no_pnl_streak: dict[str, int] = field(default_factory=dict)
    flattened_total: int = 0


class TradeSupportWrapper:
    """Always-on open-trade supervisor using the broker as source of truth."""

    def __init__(self, cfg: Any | None = None, rest: Any | None = None) -> None:
        self._explicit_cfg = cfg
        self._cfg_obj = cfg
        self.cfg = _load_cfg_block(cfg)
        self._rest = rest
        self.state = TradeSupportState()
        if not os.environ.get("APP_MODE"):
            os.environ.setdefault("APP_MODE", "DEMO")

    # -- lazy resources ---------------------------------------------------
    def _get_cfg(self) -> Any:
        if self._cfg_obj is not None:
            return self._cfg_obj
        from system.config_loader import get_config

        self._cfg_obj = get_config()
        self.cfg = _load_cfg_block(self._cfg_obj)
        return self._cfg_obj

    def _get_rest(self) -> Any:
        if self._rest is not None:
            return self._rest
        from system.credentials_loader import load_credentials
        from system.ig_rest_session import ensure_shared_authenticated

        self._rest = ensure_shared_authenticated(load_credentials())
        return self._rest

    # -- core cycle -------------------------------------------------------
    def poll_once(self) -> dict[str, Any]:
        import os as _os

        from execution.open_position_actions import execute_actions_bulk
        from execution.open_position_rules import (
            assess_open_positions,
            rows_from_ig_items,
            rows_from_snapshot_positions,
        )
        from runtime import broker_snapshot

        cfg = self._get_cfg()
        rest = self._get_rest()

        # Heartbeat before expensive arm/reconcile so operators never see a
        # multi-minute stale status while REST pressure stretches a cycle.
        try:
            snap_hb = broker_snapshot.read_snapshot(max_age_sec=None) or {}
            _write_status(
                {
                    "ts": time.time(),
                    "cycles": self.state.cycles,
                    "source": "cycle_heartbeat",
                    "broker_open": int(snap_hb.get("count") or 0),
                    "valued": 0,
                    "unvalued": int(snap_hb.get("count") or 0),
                    "total_unrealized_gbp": 0.0,
                    "by_epic": {},
                    "actions_executed": 0,
                    "actions": [],
                    "issues": ["cycle_in_progress"],
                    "flattened_total": self.state.flattened_total,
                    "edits_only_queue": {},
                    "heartbeat": True,
                }
            )
        except Exception:
            pass

        gbp_tracks = self._arm_and_tracks(rest, cfg)

        # Cross-process coordination: if another process (e.g. the agent's
        # IgPositionSync) published a very fresh broker snapshot, trust it and
        # skip our own /positions REST call. This is the core of the shared REST
        # budget — only one process actually polls when several are running.
        #
        # When the agent API is healthy, accept a slightly older shared snapshot
        # so boot hydrate is not starved — but NEVER invent an empty book from
        # "no gbp tracks". That false-flat path hid live DOW opens during smoke.
        agent_api_up = False
        try:
            import urllib.request

            with urllib.request.urlopen(
                "http://127.0.0.1:8080/api/health_light", timeout=1.5
            ) as resp:
                agent_api_up = int(getattr(resp, "status", 0) or 0) in (200, 503)
        except Exception:
            agent_api_up = False

        coord_age = float(self.cfg.get("snapshot_coordinate_age_sec", 12.0))
        if agent_api_up:
            coord_age = max(coord_age, float(self.cfg.get("agent_up_snapshot_age_sec", 30.0)))
        deferred = False
        try:
            from system.rest_api_budget import positions_poll_deferred

            deferred = bool(positions_poll_deferred())
            if deferred:
                # Under coalesce: always last-good (any age) — never require live REST.
                coord_age = None  # type: ignore[assignment]
        except Exception:
            pass
        shared = broker_snapshot.read_snapshot(max_age_sec=coord_age)
        source = "broker_rest"
        # Prefer shared snapshot when present (any pid) under pressure, or when
        # another process published a fresh book.
        use_shared = bool(shared) and (
            deferred
            or int(shared.get("pid") or 0) != _os.getpid()
            or float(shared.get("age_sec") or 999) <= 12.0
        )
        if use_shared and shared is not None:
            positions = shared.get("positions") or []
            open_mins_by_deal: dict[str, float] = {}
            rows = rows_from_snapshot_positions(
                positions, cfg, gbp_tracks=gbp_tracks
            )
            source = (
                f"last_good_snapshot({shared.get('source')})"
                if deferred
                else f"shared_snapshot({shared.get('source')})"
            )
        else:
            try:
                items = list(rest.open_positions(budget_priority=False) or [])
            except Exception as exc:
                # Never invent broker_open=0 when REST is deferred — last-good SoT.
                fallback = broker_snapshot.read_snapshot(max_age_sec=None)
                if fallback is not None:
                    positions = fallback.get("positions") or []
                    open_mins_by_deal = {}
                    rows = rows_from_snapshot_positions(
                        positions, cfg, gbp_tracks=gbp_tracks
                    )
                    source = f"last_good_snapshot({fallback.get('source')})"
                    try:
                        from system.engine_log import log_engine as _le

                        _le(
                            f"trade_support: REST deferred ({type(exc).__name__}) "
                            f"— using last-good snapshot count={len(positions)}"
                        )
                    except Exception:
                        pass
                else:
                    raise
            else:
                # open_positions may return snapshot-echo under coalesce — do not
                # stamp a fake live REST source when items are from snapshot.
                from_snap = any(
                    isinstance(it, dict) and it.get("_from_snapshot") for it in items
                )
                open_mins_by_deal = {
                    str((it.get("position") or {}).get("dealId") or "").strip(): mins
                    for it in items
                    if (mins := open_mins_from_item(it)) is not None
                }
                rows = rows_from_ig_items(
                    items,
                    cfg,
                    gbp_tracks=gbp_tracks,
                    open_mins_by_deal=open_mins_by_deal,
                )
                if from_snap:
                    source = "last_good_snapshot(ig_open_positions_echo)"
                else:
                    source = "broker_rest"
                    broker_snapshot.write_snapshot(source="trade_support", items=items)

        # Drain EDITS_ONLY close queue before assessing new breaches.
        queue_drain: dict[str, Any] = {}
        try:
            from execution.edits_only_close_queue import drain_when_tradeable, pending_count

            queue_drain = drain_when_tradeable(rest, cfg)
            queue_drain["pending"] = pending_count()
        except Exception as exc:
            queue_drain = {"error": f"{type(exc).__name__}: {exc}"}

        report = assess_open_positions(
            rows, cfg, gbp_tracks=gbp_tracks, agent_up=False, source=source
        )

        # Unmanageable guard — catch trades that never value.
        report.actions.extend(self._unmanageable_actions(rows))

        # De-dup + cap per cycle.
        actions = self._dedup_actions(report.actions)

        executed = 0
        if actions:
            report.actions = actions
            execute_actions_bulk(rest, report, cfg)
            executed = sum(1 for a in report.actions if a.ok)
            self.state.flattened_total += executed

        self.state.cycles += 1
        self.state.last_cycle_at = time.time()

        by_epic: dict[str, int] = {}
        valued = 0
        total_gbp = 0.0
        for r in rows:
            by_epic[r.epic] = by_epic.get(r.epic, 0) + 1
            if r.pnl_gbp is not None:
                valued += 1
                total_gbp += float(r.pnl_gbp)

        broker_open = len(rows)
        # SoT honesty: under coalesce/deferral never publish broker_open=0 when
        # last-good broker_snapshot still shows opens.
        snap_count = 0
        try:
            snap_lg = broker_snapshot.read_snapshot(max_age_sec=None) or {}
            snap_count = int(snap_lg.get("count") or len(snap_lg.get("positions") or []))
        except Exception:
            snap_count = 0
        coalesced = deferred or "last_good" in str(source) or "shared_snapshot" in str(source)
        if coalesced and broker_open == 0 and snap_count > 0:
            broker_open = snap_count
            source = f"{source}|sot_overlay_snapshot"
            if "broker_open_sot_overlay" not in report.issues:
                report.issues.append(
                    f"sot_overlay: status rows=0 snapshot_count={snap_count}"
                )
            try:
                log_engine(
                    f"TradeSupport: SoT overlay broker_open={snap_count} "
                    f"(rows empty under coalesce — refusing false flat)"
                )
            except Exception:
                pass

        status = {
            "ts": self.state.last_cycle_at,
            "cycles": self.state.cycles,
            "source": source,
            "broker_open": broker_open,
            "snapshot_open": snap_count,
            "coalesced": bool(coalesced or deferred),
            "valued": valued,
            "unvalued": max(0, broker_open - valued) if broker_open >= valued else len(rows) - valued,
            "total_unrealized_gbp": round(total_gbp, 2),
            "by_epic": by_epic,
            "actions_executed": executed,
            "actions": [
                {
                    "deal_id": a.deal_id,
                    "epic": a.epic,
                    "pnl_gbp": a.pnl_gbp,
                    "action": a.action,
                    "reason": a.reason,
                    "ok": a.ok,
                    "error": a.error,
                }
                for a in report.actions
            ],
            "issues": report.issues[:10],
            "flattened_total": self.state.flattened_total,
            "edits_only_queue": queue_drain,
        }
        _write_status(status)
        if report.actions:
            _audit("manage_cycle", status)
            for a in report.actions:
                log_engine(
                    f"TradeSupport: {a.deal_id[:12]} {a.action} {a.reason} "
                    f"[{'ok' if a.ok else a.error or 'pending'}]"
                )
        return status

    def _arm_and_tracks(self, rest: Any, cfg: Any) -> dict[str, Any]:
        """Return current GBP tracks; run the heavy reconcile only periodically.

        Reading ``micro_gbp_exit`` tracks is in-memory/cheap and done every cycle.
        The full risk-stack reconcile (per-position REST) is expensive under the
        IG budget, so it runs every Nth cycle unless ``arm_stack_every_cycle`` is
        set — the fast value+assess+flatten safety path is never blocked waiting
        on it. Trail peak/floor updates run every cycle via ``on_watchdog_tick``.
        """
        gbp_tracks: dict[str, Any] = {}
        every_cycle = bool(self.cfg.get("arm_stack_every_cycle", False))
        every_n = int(self.cfg.get("arm_every_n_cycles", 5) or 1)
        do_reconcile = every_cycle or every_n <= 1 or (self.state.cycles % every_n == 0)
        try:
            from runtime.micro_gbp_exit import on_watchdog_tick, snapshot as gbp_snap
            from runtime.micro_gbp_exit import start_micro_gbp_exit_engine
            from runtime.dynamic_limit_engine import start_dynamic_limit_engine
            from runtime.virtual_stop_loss import start_virtual_stop_watchdog

            start_micro_gbp_exit_engine(rest)
            start_virtual_stop_watchdog(rest)
            start_dynamic_limit_engine()
            if do_reconcile:
                from execution.position_risk_stack import (
                    reconcile_open_positions_risk_stack,
                )

                reconcile_open_positions_risk_stack(rest, cfg=cfg, force=True)
            on_watchdog_tick()
            gbp_tracks = gbp_snap().get("tracks") or {}
        except Exception as exc:
            self.state.last_error = f"arm:{type(exc).__name__}: {exc}"
            _audit("arm_error", {"error": self.state.last_error})
        return gbp_tracks

    def _unmanageable_actions(self, rows: list[Any]) -> list[Any]:
        from execution.open_position_rules import ManageAction

        threshold = int(self.cfg.get("unmanageable_cycles", 5))
        min_age = float(self.cfg.get("unmanageable_min_age_min", 10.0))
        flatten = bool(self.cfg.get("flatten_unmanageable", True))

        live_ids = {r.deal_id for r in rows}
        # Forget streaks for trades no longer open.
        self.state.no_pnl_streak = {
            d: n for d, n in self.state.no_pnl_streak.items() if d in live_ids
        }

        actions: list[Any] = []
        for row in rows:
            if row.pnl_gbp is not None:
                self.state.no_pnl_streak.pop(row.deal_id, None)
                continue
            streak = self.state.no_pnl_streak.get(row.deal_id, 0) + 1
            self.state.no_pnl_streak[row.deal_id] = streak
            aged = row.open_mins is not None and row.open_mins >= min_age
            if streak >= threshold and aged:
                _audit(
                    "unmanageable",
                    {
                        "deal_id": row.deal_id,
                        "epic": row.epic,
                        "streak": streak,
                        "open_mins": row.open_mins,
                        "flatten": flatten,
                    },
                )
                if flatten:
                    actions.append(
                        ManageAction(
                            deal_id=row.deal_id,
                            epic=row.epic,
                            pnl_gbp=0.0,
                            action="flatten",
                            reason=(
                                f"unmanageable no_pnl x{streak} "
                                f"age={row.open_mins:.0f}m"
                            ),
                        )
                    )
        return actions

    def _dedup_actions(self, actions: list[Any]) -> list[Any]:
        seen: set[str] = set()
        out: list[Any] = []
        cap = int(self.cfg.get("max_actions_per_cycle", 8))
        for a in actions:
            if a.deal_id in seen:
                continue
            seen.add(a.deal_id)
            out.append(a)
            if len(out) >= cap:
                break
        return out

    def run(self) -> None:
        global _stop
        interval = max(5.0, float(self.cfg.get("poll_interval_sec", 12.0)))
        log_engine(f"TradeSupport: wrapper armed poll={interval}s")
        _audit("wrapper_start", {"poll_interval_sec": interval})
        while not _stop:
            try:
                self.poll_once()
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"
                _audit("poll_error", {"error": self.state.last_error})
                log_engine(f"TradeSupport: poll error {self.state.last_error}")
            time.sleep(interval)
        _audit("wrapper_stop", {"cycles": self.state.cycles})


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    log_engine(f"TradeSupport: signal {signum} — stopping")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trade Support Wrapper daemon")
    parser.add_argument("--once", action="store_true", help="Run a single cycle")
    parser.add_argument("--poll-sec", type=float, default=None)
    args = parser.parse_args(argv)

    wrapper = TradeSupportWrapper()
    if args.poll_sec is not None:
        wrapper.cfg["poll_interval_sec"] = max(5.0, float(args.poll_sec))

    if not wrapper.cfg.get("enabled", True):
        print("trade_support_wrapper disabled in config", file=sys.stderr)
        return 0

    if args.once:
        print(json.dumps(wrapper.poll_once(), indent=2, default=str))
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    wrapper.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
