"""Gate 5 fast-stream hydration — REST snapshot fallback when the inbound socket is quiet."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

FAST_HYDRATION_WAIT_SEC = 5.0
_HYDRATION_POLL_SEC = 0.25
_QUOTE_MAX_AGE_SEC = 45.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hub_has_fresh_tick(epics: list[str], *, max_age_sec: float = _QUOTE_MAX_AGE_SEC) -> str | None:
    hub = get_market_data_hub()
    for epic in epics:
        snap = hub.get_snapshot(epic)
        if snap is None or snap.bid <= 0 or snap.offer <= 0:
            continue
        if snap.age_seconds() <= max_age_sec:
            return epic
    return None


def resolve_hydration_epics(
    *,
    cfg: Any | None = None,
    epics: list[str] | None = None,
) -> list[str]:
    """Core night-matrix epics intersected with enabled instruments when config is available."""
    candidates = [str(e).strip() for e in (epics or []) if str(e).strip()]
    if not candidates and cfg is not None:
        try:
            from trading.instrument_registry import InstrumentRegistry

            reg = InstrumentRegistry(cfg.as_dict())
            candidates = [
                str(inst.get("epic") or "").strip()
                for _iid, inst in reg.get_enabled_with_ids()
                if str(inst.get("epic") or "").strip()
            ]
        except Exception:
            candidates = []
    if not candidates:
        candidates = list(NIGHT_MATRIX_EPICS)
    night = set(NIGHT_MATRIX_EPICS)
    preferred = [e for e in candidates if e in night]
    return preferred or candidates


def _inject_rest_quotes(rest_client: Any, epics: list[str]) -> tuple[list[str], str | None]:
    if rest_client is None:
        return [], None

    hub = get_market_data_hub()
    hub.attach_rest(rest_client)
    hydrated: list[str] = []
    first_epic: str | None = None

    for epic in epics:
        bid = offer = 0.0
        try:
            if hasattr(rest_client, "fetch_live_prices"):
                bid, offer = rest_client.fetch_live_prices(epic, budget_priority=True)
            elif hasattr(rest_client, "fetch_market_snapshot"):
                snap = rest_client.fetch_market_snapshot(epic, live=True, budget_priority=True)
                bid = float(snap.get("bid") or 0)
                offer = float(snap.get("offer") or 0)
            else:
                continue
        except Exception as exc:
            log_engine(
                f"fast_stream_hydration: REST snapshot failed epic={epic} "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if bid <= 0 or offer <= 0:
            continue
        published = hub.publish(epic, bid, offer, source="rest")
        if published is not None:
            hydrated.append(epic)
            if first_epic is None:
                first_epic = epic

    return hydrated, first_epic


def fast_stream_hydration_fallback(
    rest_client: Any,
    *,
    cfg: Any | None = None,
    epics: list[str] | None = None,
    wait_sec: float = FAST_HYDRATION_WAIT_SEC,
) -> dict[str, Any]:
    """
    Wait briefly for inbound hub ticks; on timeout fetch GET /markets and publish.

    Returns a dict with ``mode`` of ``STREAM`` (natural tick) or ``LIVE_FALLBACK`` (REST).
    """
    targets = resolve_hydration_epics(cfg=cfg, epics=epics)
    deadline = time.monotonic() + max(0.5, float(wait_sec))
    first_epic: str | None = None

    while time.monotonic() < deadline:
        first_epic = _hub_has_fresh_tick(targets)
        if first_epic:
            try:
                from system.stream_ready import is_stream_ready, signal_stream_ready

                if not is_stream_ready():
                    signal_stream_ready(source=f"fast_hydration:stream:{first_epic}")
            except Exception:
                pass
            log_engine(
                f"fast_stream_hydration: stream live epic={first_epic} "
                f"(waited <{wait_sec:.0f}s)"
            )
            return {
                "mode": "STREAM",
                "hydrated_epics": [first_epic],
                "first_tick_epic": first_epic,
                "first_tick_at": _utc_now_iso(),
                "wait_sec": wait_sec,
            }
        time.sleep(_HYDRATION_POLL_SEC)

    log_engine(
        f"fast_stream_hydration: no hub tick within {wait_sec:.0f}s — "
        f"REST GET /markets for {len(targets)} epic(s)"
    )
    hydrated, first_epic = _inject_rest_quotes(rest_client, targets)
    if hydrated:
        try:
            from system.stream_ready import signal_stream_ready

            signal_stream_ready(source="fast_hydration:live_fallback")
        except Exception:
            pass
        log_engine(
            f"fast_stream_hydration: LIVE_FALLBACK hydrated {len(hydrated)} epic(s) "
            f"first={first_epic}"
        )
        return {
            "mode": "LIVE_FALLBACK",
            "hydrated_epics": hydrated,
            "first_tick_epic": first_epic,
            "first_tick_at": _utc_now_iso(),
            "wait_sec": wait_sec,
        }

    log_engine("fast_stream_hydration: LIVE_FALLBACK failed — no REST quotes published")
    return {
        "mode": "FAILED",
        "hydrated_epics": [],
        "first_tick_epic": None,
        "first_tick_at": None,
        "wait_sec": wait_sec,
    }
