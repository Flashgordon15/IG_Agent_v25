"""
Cross-process dashboard snapshot — atomic JSON file + in-process subscribers.

Trading loop (separate process) calls publish_tick(); FastAPI reads the file
and broadcasts to WebSocket clients. Trading continues if the API process fails.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.snapshot import (
    _iso_now,
    build_default_tick,
    enrich_signal_thresholds,
    normalize_tick,
)
from data.models import Quote
from system.paths import data_dir
from system.state_manager import atomic_write_json, read_json_file
from trading.open_position_view import enrich_positions_with_quote

_SNAPSHOT_FILENAME = "dashboard_snapshot.json"
_lock = threading.RLock()
_cached: dict[str, Any] = build_default_tick()
_cached_mtime: float = 0.0
_last_ws_broadcast_mtime: float = 0.0
_path_override: Path | None = None
_subscribers: list[Callable[[dict[str, Any]], None]] = []
_last_hub_push_ts: float = 0.0
_hub_push_min_interval: float = 0.25

# Last-known-good caches for fields computed fresh in _tick_for_readers().
# Using a cached value prevents momentary None/missing from causing UI flicker.
_cached_uptime: str | None = None
_cached_position_sync_status: str | None = None
_cached_ohlc_markets_cached: int | None = None


def snapshot_path() -> Path:
    if _path_override is not None:
        return _path_override
    state_dir = data_dir() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _SNAPSHOT_FILENAME


def set_snapshot_path_for_tests(path: Path | str | None) -> None:
    global _path_override, _cached, _cached_mtime
    with _lock:
        _path_override = Path(path) if path else None
        _cached = build_default_tick()
        _cached_mtime = 0.0


def reset_snapshot_store_for_tests() -> None:
    global \
        _cached, \
        _cached_mtime, \
        _subscribers, \
        _last_hub_push_ts, \
        _hub_push_min_interval
    global _cached_uptime, _cached_position_sync_status, _cached_ohlc_markets_cached
    with _lock:
        _path_override = None
        _cached = build_default_tick()
        _cached_mtime = 0.0
        _subscribers.clear()
        _last_hub_push_ts = 0.0
        _hub_push_min_interval = 0.25
        _cached_uptime = None
        _cached_position_sync_status = None
        _cached_ohlc_markets_cached = None


def subscribe(callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Register a tick listener; returns unsubscribe function."""
    with _lock:
        _subscribers.append(callback)

    def _unsub() -> None:
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return _unsub


def _notify_locked(tick: dict[str, Any]) -> None:
    """Push a JSON-safe copy to WebSocket subscribers (may run on engine thread)."""
    payload = json.loads(json.dumps(_tick_for_readers(tick), default=str))
    for cb in list(_subscribers):
        try:
            cb(payload)
        except Exception:
            pass


def write_tick_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Persist tick snapshot to disk and notify in-process WebSocket subscribers.

    A background file watcher still polls for cross-process writers; when the
    trading loop runs inside the API process (v25 main.py), we push WS updates
    immediately so the dashboard tracks live stream prices.
    """
    global _cached, _cached_mtime
    tick = normalize_tick(payload)
    path = snapshot_path()
    atomic_write_json(path, tick)
    with _lock:
        _cached = tick
        try:
            _cached_mtime = path.stat().st_mtime
        except OSError:
            _cached_mtime = time.time()
    return tick


def push_hub_quote_to_dashboard(
    epic: str,
    bid: float,
    offer: float,
    *,
    tick_age_s: float | None = None,
    stream_status: str = "LIVE",
) -> None:
    """Merge live hub bid/offer into the dashboard tick and push to WebSocket clients."""
    global _last_hub_push_ts
    if bid <= 0 or offer <= 0:
        return
    epic_key = str(epic or "").strip()
    if not epic_key:
        return
    now = time.time()
    if now - _last_hub_push_ts < _hub_push_min_interval:
        return
    _last_hub_push_ts = now

    tick = dict(get_tick())
    sig = tick.get("signal")
    if isinstance(sig, dict):
        tick["signal"] = dict(sig)

    spread = round(float(offer) - float(bid), 5)
    age = round(float(tick_age_s), 1) if tick_age_s is not None else 0.0
    ts = _iso_now()

    markets = tick.get("markets")
    if isinstance(markets, dict):
        next_markets = {
            k: dict(v) if isinstance(v, dict) else v for k, v in markets.items()
        }
        slice_tick = dict(next_markets.get(epic_key) or {})
        slice_tick["epic"] = epic_key
        slice_tick["bid"] = float(bid)
        slice_tick["offer"] = float(offer)
        slice_tick["spread"] = spread
        slice_tick["tick_age_s"] = age
        slice_tick["stream_status"] = stream_status
        slice_tick["ts"] = ts
        if slice_tick.get("market_state") == "OFFLINE":
            slice_tick["market_state"] = "OPEN"
        next_markets[epic_key] = slice_tick
        tick["markets"] = next_markets

    top_epic = str(tick.get("selected_epic") or tick.get("epic") or "")
    if not top_epic or top_epic == epic_key:
        tick["bid"] = float(bid)
        tick["offer"] = float(offer)
        tick["spread"] = spread
        tick["tick_age_s"] = age
        tick["stream_status"] = stream_status
        tick["ts"] = ts
        if tick.get("market_state") == "OFFLINE" and bid > 0:
            tick["market_state"] = "OPEN"

    _refresh_positions_from_hub_quote(tick, epic_key, float(bid), float(offer))
    publish_tick(tick, notify=True)


def _point_value_gbp_for_epic(tick: dict[str, Any], epic: str) -> float:
    markets = tick.get("markets")
    if isinstance(markets, dict):
        slice_tick = markets.get(epic)
        if isinstance(slice_tick, dict):
            raw = slice_tick.get("ig_point_value_gbp")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
    raw = tick.get("ig_point_value_gbp")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return 1.0


def _refresh_positions_from_hub_quote(
    tick: dict[str, Any],
    epic: str,
    bid: float,
    offer: float,
) -> None:
    """Update open-position mark/pnl_pts from streaming quote between loop snapshots."""
    positions = tick.get("positions")
    if not isinstance(positions, list) or not positions:
        return
    quote = Quote(datetime.now(timezone.utc), bid, offer)
    tick["positions"] = enrich_positions_with_quote(
        positions,
        quote,
        point_value_gbp=_point_value_gbp_for_epic(tick, epic),
        epic=epic,
    )


def wire_hub_quotes_to_dashboard(*, min_interval: float = 0.25) -> Callable[[], None]:
    """Subscribe MarketDataHub publishes → dashboard WebSocket (Lightstreamer path)."""
    global _hub_push_min_interval
    _hub_push_min_interval = max(0.1, float(min_interval))

    from system.market_data_hub import on_hub_quote

    def _on_hub(snap: Any) -> None:
        push_hub_quote_to_dashboard(
            str(getattr(snap, "epic", "") or ""),
            float(snap.bid),
            float(snap.offer),
            tick_age_s=float(snap.age_seconds()),
        )

    return on_hub_quote(_on_hub)


def publish_tick(payload: dict[str, Any], *, notify: bool = True) -> dict[str, Any]:
    """Write snapshot and optionally notify in-process subscribers (tests)."""
    global _last_ws_broadcast_mtime
    tick = write_tick_snapshot(payload)
    if notify:
        with _lock:
            try:
                _last_ws_broadcast_mtime = snapshot_path().stat().st_mtime
            except OSError:
                _last_ws_broadcast_mtime = time.time()
            _notify_locked(tick)
    return tick


def _tick_for_readers(tick: dict[str, Any]) -> dict[str, Any]:
    """Copy + enrich so WebSocket/poll always expose threshold fields."""
    out = dict(tick)
    sig = out.get("signal")
    if isinstance(sig, dict):
        out["signal"] = dict(sig)
    enrich_signal_thresholds(out)

    # Aggregate positions from all market slices into the top-level positions array
    # so the dashboard TradesPanel always has a flat list to render.
    markets = out.get("markets")
    if isinstance(markets, dict):
        from trading.open_position_view import epic_market_label, normalize_epic

        all_positions: list[dict] = []
        for epic_key, mslice in markets.items():
            if not isinstance(mslice, dict):
                continue
            slice_epic = normalize_epic(epic_key)
            slice_market = str(
                mslice.get("market_name")
                or mslice.get("market")
                or epic_market_label(slice_epic)
                or slice_epic
            )
            for pos in mslice.get("positions") or []:
                if isinstance(pos, dict):
                    enriched = dict(pos)
                    pos_epic = normalize_epic(str(enriched.get("epic") or slice_epic))
                    enriched["epic"] = pos_epic or slice_epic
                    enriched["market"] = str(
                        enriched.get("market")
                        or slice_market
                        or epic_market_label(enriched["epic"])
                    )
                    if pos_epic and slice_epic and pos_epic != slice_epic:
                        continue
                    all_positions.append(enriched)
        if all_positions:
            out["positions"] = all_positions
    # Inject live OHLC market count so SystemPanel can show it without an extra API call
    if "ohlc_markets_cached" not in out:
        global _cached_ohlc_markets_cached
        try:
            from system.paths import data_dir as _data_dir

            ohlc_dir = _data_dir() / "ohlc_cache"
            count = sum(
                1
                for f in ohlc_dir.iterdir()
                if f.suffix == ".jsonl" and not f.name.endswith(".synthetic")
            )
            _cached_ohlc_markets_cached = count
            out["ohlc_markets_cached"] = count
        except Exception:
            if _cached_ohlc_markets_cached is not None:
                out["ohlc_markets_cached"] = _cached_ohlc_markets_cached

    # ML model metadata for SystemPanel — always populate so dashboard never flickers
    try:
        import json as _json
        from datetime import datetime as _dt

        from system.paths import data_dir as _data_dir

        meta_file = _data_dir() / "ml_model" / "meta.json"
        if meta_file.exists():
            meta = _json.loads(meta_file.read_text())
            out["model_version"] = meta.get("version") or meta.get("trained_at") or "—"
            out["last_retrain_time"] = meta.get("trained_at") or "—"
        else:
            model_file = _data_dir() / "ml_model" / "model.pkl"
            if model_file.exists():
                mtime = model_file.stat().st_mtime
                ts = _dt.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                out["model_version"] = ts
                out["last_retrain_time"] = ts
            else:
                out.setdefault("model_version", "No model")
                out.setdefault("last_retrain_time", "—")
    except Exception:
        out.setdefault("model_version", "—")
        out.setdefault("last_retrain_time", "—")

    # Agent uptime derived from lock-file mtime
    if not out.get("uptime"):
        global _cached_uptime
        try:
            import time as _time

            from system.paths import data_dir as _data_dir

            lock = _data_dir() / ".ig_agent_v25.lock"
            if lock.exists():
                secs = int(_time.time() - lock.stat().st_mtime)
                h, m = divmod(secs // 60, 60)
                uptime_str = f"{h}h {m:02d}m" if h else f"{m}m {secs % 60:02d}s"
                _cached_uptime = uptime_str
                out["uptime"] = uptime_str
            elif _cached_uptime is not None:
                out["uptime"] = _cached_uptime
        except Exception:
            if _cached_uptime is not None:
                out["uptime"] = _cached_uptime

    # Position sync status from diagnostics snapshot
    if not out.get("position_sync_status"):
        global _cached_position_sync_status
        try:
            from system.demo_execution_trace import get_demo_diagnostics_snapshot

            diag = get_demo_diagnostics_snapshot()
            status = diag.ig_position_sync_status
            if status:
                _cached_position_sync_status = status
                out["position_sync_status"] = status
            elif _cached_position_sync_status is not None:
                out["position_sync_status"] = _cached_position_sync_status
        except Exception:
            if _cached_position_sync_status is not None:
                out["position_sync_status"] = _cached_position_sync_status

    return out


def get_tick() -> dict[str, Any]:
    """Return latest snapshot (memory cache, refreshed from disk if newer)."""
    global _cached, _cached_mtime
    path = snapshot_path()
    with _lock:
        if not path.exists():
            return _tick_for_readers(_cached)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return _tick_for_readers(_cached)
        if mtime <= _cached_mtime:
            return _tick_for_readers(_cached)
    data = read_json_file(path)
    if not isinstance(data, dict):
        return _tick_for_readers(_cached)
    tick = normalize_tick(data)
    with _lock:
        _cached = tick
        _cached_mtime = mtime
    return _tick_for_readers(tick)


def snapshot_age_s() -> float | None:
    """Seconds since last published tick, or None if never written."""
    tick = get_tick()
    ts = tick.get("ts")
    if not ts:
        return None
    try:
        from datetime import datetime

        if str(ts).endswith("Z"):
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(str(ts))
        return max(0.0, time.time() - dt.timestamp())
    except Exception:
        return None


async def watch_snapshot_file(poll_interval: float = 0.25) -> None:
    """Poll snapshot file for cross-process updates (API-only process)."""
    global _cached, _cached_mtime, _last_ws_broadcast_mtime
    path = snapshot_path()
    while True:
        await asyncio.sleep(poll_interval)
        if not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        with _lock:
            if mtime <= _last_ws_broadcast_mtime:
                continue
        data = read_json_file(path)
        if not isinstance(data, dict):
            continue
        tick = normalize_tick(data)
        with _lock:
            _cached = tick
            _cached_mtime = mtime
            _last_ws_broadcast_mtime = mtime
            _notify_locked(tick)
