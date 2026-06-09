"""Spread-to-ATR Friction Matrix and shadow counterfactual learning — §18.4."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from signals.indicators import atr
from system.paths import data_dir
from trading.ohlc_cache_paths import ohlc_cache_path

FRICTION_WARN_RATIO = 0.15
ATR_PERIOD = 14
SHADOW_LEARNING_OFFSET_KEY = "shadow_learning_byte_offset"
SHADOW_FORWARD_BARS = 48
SHADOW_MIN_ATR = 0.01


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _load_recent_bars(epic: str, *, limit: int = ATR_PERIOD + 5) -> pd.DataFrame:
    path = ohlc_cache_path(epic)
    if not path.exists():
        return pd.DataFrame(columns=["high", "low", "close"])
    rows: list[dict[str, float]] = []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        for line in lines[-limit:]:
            if not line.strip():
                continue
            obj = json.loads(line)
            rows.append(
                {
                    "high": float(obj.get("high") or obj.get("h") or 0),
                    "low": float(obj.get("low") or obj.get("l") or 0),
                    "close": float(obj.get("close") or obj.get("c") or 0),
                }
            )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return pd.DataFrame(columns=["high", "low", "close"])
    return pd.DataFrame(rows)


def active_14_bar_atr(epic: str) -> float | None:
    df = _load_recent_bars(epic, limit=ATR_PERIOD + 5)
    if len(df) < ATR_PERIOD:
        return None
    series = atr(df, period=ATR_PERIOD)
    if series.empty:
        return None
    val = float(series.iloc[-1])
    return val if val > 0 else None


def parse_live_spread_pts(
    *,
    bid: float | None,
    offer: float | None,
    spread_pts: float | None = None,
) -> float | None:
    if spread_pts is not None:
        try:
            v = float(spread_pts)
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    if bid is None or offer is None:
        return None
    try:
        v = abs(float(offer) - float(bid))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass
class FrictionCell:
    epic: str
    spread_pts: float | None
    atr_14_pts: float | None
    spread_friction_ratio: float | None
    warning: bool
    prohibited: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "spread_pts": self.spread_pts,
            "atr_14_pts": self.atr_14_pts,
            "spread_friction_ratio": self.spread_friction_ratio,
            "spread_friction_pct": (
                round(self.spread_friction_ratio * 100, 2)
                if self.spread_friction_ratio is not None
                else None
            ),
            "warning": self.warning,
            "prohibited": self.prohibited,
            "detail": self.detail,
        }


def friction_warning(
    epic: str,
    *,
    bid: float | None = None,
    offer: float | None = None,
    spread_pts: float | None = None,
    atr_pts: float | None = None,
) -> FrictionCell:
    """Return friction assessment; warn when spread/ATR > 0.15 (§18.4)."""
    spread = parse_live_spread_pts(bid=bid, offer=offer, spread_pts=spread_pts)
    atr_v = atr_pts if atr_pts is not None else active_14_bar_atr(epic)
    ratio: float | None = None
    warning = False
    prohibited = False
    detail = "ok"

    if spread is None or atr_v is None or atr_v <= 0:
        detail = "insufficient_data"
    else:
        ratio = spread / atr_v
        if ratio > FRICTION_WARN_RATIO:
            warning = True
            prohibited = True
            detail = f"spread/ATR {ratio:.3f} exceeds {FRICTION_WARN_RATIO:.2f}"

    return FrictionCell(
        epic=epic,
        spread_pts=spread,
        atr_14_pts=atr_v,
        spread_friction_ratio=ratio,
        warning=warning,
        prohibited=prohibited,
        detail=detail,
    )


def build_friction_matrix(
    epics: list[str],
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build friction matrix for proposal packaging."""
    quotes = quotes or {}
    cells: list[dict[str, Any]] = []
    for epic in epics:
        q = quotes.get(epic) or {}
        cell = friction_warning(
            epic,
            bid=q.get("bid"),
            offer=q.get("offer"),
            spread_pts=q.get("spread_pts"),
        )
        cells.append(cell.to_dict())

    any_prohibited = any(c.get("prohibited") for c in cells)
    return {
        "ts": _utc_now(),
        "threshold_ratio": FRICTION_WARN_RATIO,
        "atr_period": ATR_PERIOD,
        "cells": cells,
        "eligible": not any_prohibited,
    }


def read_quotes_from_dashboard_snapshot() -> dict[str, dict[str, Any]]:
    snap_path = data_dir() / "state" / "dashboard_snapshot.json"
    if not snap_path.exists():
        return {}
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    markets = snap.get("markets") or {}
    out: dict[str, dict[str, Any]] = {}
    if isinstance(markets, dict):
        for epic, m in markets.items():
            if isinstance(m, dict):
                out[str(epic)] = {
                    "bid": m.get("bid"),
                    "offer": m.get("offer"),
                    "spread_pts": m.get("spread_pts"),
                }
    return out


# ---------------------------------------------------------------------------
# Shadow learning pipeline — counterfactual labels → learning_db.setup_stats
# ---------------------------------------------------------------------------


def shadow_log_path() -> Path:
    return data_dir() / "shadow_log.jsonl"


def shadow_log_paths() -> list[Path]:
    primary = shadow_log_path()
    paths = [primary]
    rotated = primary.with_suffix(".jsonl.1")
    if rotated.is_file():
        paths.append(rotated)
    return paths


def _row_matches_day(row: dict[str, Any], day: str) -> bool:
    ts = str(row.get("timestamp") or "")
    if ts.startswith(day):
        return True
    parsed = _parse_shadow_ts(ts)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%d") == day
    return False


def _parse_shadow_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _market_epic_map() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        from system.config_loader import ConfigLoader

        for row in ConfigLoader().get_markets():
            name = str(row.get("name") or "").strip()
            epic = str(row.get("epic") or "").strip()
            if name and epic:
                out[name] = epic
        cfg = __import__("system.config_loader", fromlist=["get_config"]).get_config()
        instruments = getattr(cfg, "instruments", None) or {}
        if isinstance(instruments, dict):
            for spec in instruments.values():
                if not isinstance(spec, dict):
                    continue
                name = str(spec.get("name") or "").strip()
                epic = str(spec.get("epic") or "").strip()
                if name and epic:
                    out[name] = epic
    except Exception:
        pass
    return out


def _load_ohlc_rows(epic: str, market: str = "") -> list[dict[str, Any]]:
    path = ohlc_cache_path(epic, market=market)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            ts_raw = obj.get("time") or obj.get("timestamp") or obj.get("t")
            ts = _parse_shadow_ts(str(ts_raw)) if ts_raw else None
            if ts is None and ts_raw:
                for fmt in ("%Y/%m/%d:%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        ts = datetime.strptime(str(ts_raw), fmt).replace(
                            tzinfo=timezone.utc
                        )
                        break
                    except ValueError:
                        continue
            close = float(obj.get("close") or obj.get("c") or obj.get("mid") or 0)
            high = float(obj.get("high") or obj.get("h") or close)
            low = float(obj.get("low") or obj.get("l") or close)
            if close <= 0:
                continue
            rows.append({"time": ts, "close": close, "high": high, "low": low})
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []
    rows.sort(key=lambda r: r["time"] or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def _bars_after(
    rows: list[dict[str, Any]], after: datetime, *, limit: int = SHADOW_FORWARD_BARS
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        ts = row.get("time")
        if ts is None or ts <= after:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _entry_at(rows: list[dict[str, Any]], at: datetime) -> float | None:
    best: dict[str, Any] | None = None
    for row in rows:
        ts = row.get("time")
        if ts is None:
            continue
        if ts <= at:
            best = row
        else:
            break
    if best is None and rows:
        best = rows[0]
    return float(best["close"]) if best else None


def _shadow_side(row: dict[str, Any]) -> str | None:
    direction = str(row.get("direction") or "").upper()
    if direction in ("BUY", "SELL"):
        return direction
    setup = str(row.get("setup_key") or "")
    head = setup.split("|", 1)[0].upper() if setup else ""
    return head if head in ("BUY", "SELL") else None


def simulate_shadow_outcome(
    *,
    side: str,
    entry: float,
    atr_pts: float,
    forward_bars: list[dict[str, Any]],
    stop_mult: float = 2.5,
    reward_mult: float = 2.0,
    stop_floor: float = 0.0,
    stop_cap: float = 9999.0,
) -> tuple[str, float]:
    """Walk forward bars — first touch of stop or target wins."""
    if entry <= 0 or atr_pts < SHADOW_MIN_ATR or not forward_bars:
        return "BREAKEVEN", 0.0
    stop_dist = max(stop_floor, min(stop_cap, atr_pts * stop_mult))
    if side == "BUY":
        stop_px = entry - stop_dist
        target_px = entry + stop_dist * reward_mult
        for bar in forward_bars:
            low = float(bar.get("low") or bar.get("close") or entry)
            high = float(bar.get("high") or bar.get("close") or entry)
            if low <= stop_px:
                return "LOSS", -stop_dist
            if high >= target_px:
                return "WIN", stop_dist * reward_mult
    else:
        stop_px = entry + stop_dist
        target_px = entry - stop_dist * reward_mult
        for bar in forward_bars:
            low = float(bar.get("low") or bar.get("close") or entry)
            high = float(bar.get("high") or bar.get("close") or entry)
            if high >= stop_px:
                return "LOSS", -stop_dist
            if low <= target_px:
                return "WIN", stop_dist * reward_mult
    return "BREAKEVEN", 0.0


@dataclass
class ShadowLearningResult:
    processed: int = 0
    ingested: int = 0
    skipped: int = 0
    errors: int = 0
    byte_offset: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "ingested": self.ingested,
            "skipped": self.skipped,
            "errors": self.errors,
            "byte_offset": self.byte_offset,
            "detail": self.detail,
        }


def process_shadow_learning_pipeline(
    memory_store: Any,
    *,
    shadow_path: Path | None = None,
    shadow_paths: list[Path] | None = None,
    max_rows: int = 500,
    persist_offset: bool = True,
    include_fired: bool = False,
    include_skipped: bool = True,
    day_filter: str | None = None,
    reset_offset: bool = False,
) -> ShadowLearningResult:
    """
    Read shadow_log rows, label counterfactual outcomes from OHLC forward paths,
    and write into learning_db.setup_stats.
    """
    result = ShadowLearningResult()
    paths = list(shadow_paths or [])
    if shadow_path is not None:
        paths = [shadow_path]
    if not paths:
        paths = shadow_log_paths()
    paths = [p for p in paths if p.is_file()]
    if not paths:
        result.detail = "shadow_log missing"
        return result

    if reset_offset and memory_store is not None:
        try:
            memory_store.clear_runtime_state(SHADOW_LEARNING_OFFSET_KEY)
        except Exception:
            pass

    cfg = None
    try:
        from system.config_loader import get_config

        cfg = get_config()
    except Exception:
        pass
    stop_mult = float(getattr(cfg, "atr_multiplier", 2.5) or 2.5) if cfg else 2.5
    reward_mult = float(getattr(cfg, "reward_multiple", 2.0) or 2.0) if cfg else 2.0
    stop_floor = float(getattr(cfg, "adaptive_min_risk_points", 0) or 0) if cfg else 0.0
    stop_cap = (
        float(getattr(cfg, "adaptive_max_risk_points", 9999) or 9999) if cfg else 9999.0
    )

    offset = 0
    if persist_offset and not reset_offset and memory_store is not None:
        try:
            saved = memory_store.get_runtime_state(SHADOW_LEARNING_OFFSET_KEY)
            offset = int(saved) if saved else 0
        except Exception:
            offset = 0
    primary = paths[0]
    offset = (
        min(offset, primary.stat().st_size)
        if persist_offset and not reset_offset
        else 0
    )

    market_epic = _market_epic_map()
    ohlc_cache: dict[str, list[dict[str, Any]]] = {}
    lines: list[str] = []

    if persist_offset and not reset_offset and len(paths) == 1:
        with open(primary, encoding="utf-8") as fh:
            fh.seek(offset)
            chunk = fh.read()
            result.byte_offset = fh.tell()
        lines = chunk.splitlines()
    else:
        for path in paths:
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
        if persist_offset and primary.is_file():
            result.byte_offset = primary.stat().st_size

    for line in lines:
        if result.processed >= max_rows:
            break
        line = line.strip()
        if not line:
            continue
        result.processed += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            result.errors += 1
            continue
        if not isinstance(row, dict):
            result.skipped += 1
            continue
        if day_filter and not _row_matches_day(row, day_filter):
            result.skipped += 1
            continue
        fired = bool(row.get("would_have_fired"))
        if fired and not include_fired:
            result.skipped += 1
            continue
        if not fired and not include_skipped:
            result.skipped += 1
            continue
        setup_key = str(row.get("setup_key") or "").strip()
        if not setup_key or setup_key.startswith("WAIT"):
            result.skipped += 1
            continue
        side = _shadow_side(row)
        if not side:
            result.skipped += 1
            continue
        ts = _parse_shadow_ts(str(row.get("timestamp") or ""))
        if ts is None:
            result.skipped += 1
            continue
        market = str(row.get("market") or "")
        epic = market_epic.get(market, "")
        if not epic:
            epic = market
        cache_key = f"{epic}|{market}"
        if cache_key not in ohlc_cache:
            ohlc_cache[cache_key] = _load_ohlc_rows(epic, market=market)
        bars = ohlc_cache[cache_key]
        if not bars:
            result.skipped += 1
            continue
        entry = _entry_at(bars, ts)
        if entry is None:
            result.skipped += 1
            continue
        atr_pts = float(row.get("atr") or 0)
        if atr_pts < SHADOW_MIN_ATR:
            result.skipped += 1
            continue
        forward = _bars_after(bars, ts)
        if not forward:
            result.skipped += 1
            continue
        label, pnl_pts = simulate_shadow_outcome(
            side=side,
            entry=entry,
            atr_pts=atr_pts,
            forward_bars=forward,
            stop_mult=stop_mult,
            reward_mult=reward_mult,
            stop_floor=stop_floor,
            stop_cap=stop_cap,
        )
        if memory_store is None:
            result.skipped += 1
            continue
        ref_tag = "FIRED-" if fired else "SKIP-"
        try:
            ok = memory_store.ingest_shadow_counterfactual(
                setup_key=setup_key,
                market=market,
                epic=epic,
                side=side,
                pnl_points=pnl_pts,
                result=label,
                shadow_ts=str(row.get("timestamp") or ts.isoformat()),
                confidence=float(
                    row.get("adjusted_score") or row.get("confidence") or 0
                ),
                entry=entry,
                ref_tag=ref_tag,
            )
            if ok:
                result.ingested += 1
            else:
                result.skipped += 1
        except Exception:
            result.errors += 1

    if persist_offset and memory_store is not None:
        try:
            memory_store.set_runtime_state(
                SHADOW_LEARNING_OFFSET_KEY, str(result.byte_offset)
            )
        except Exception:
            pass

    result.detail = (
        f"shadow learning: {result.ingested} ingested / "
        f"{result.processed} processed (offset {result.byte_offset})"
    )
    return result


def force_shadow_learning_pipeline(
    memory_store: Any,
    *,
    day: str | None = None,
) -> ShadowLearningResult:
    """
    Process tonight's full shadow log (skipped + fired) and refresh live setup stats.
    """
    day_key = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = process_shadow_learning_pipeline(
        memory_store,
        shadow_paths=shadow_log_paths(),
        max_rows=50_000,
        persist_offset=True,
        include_fired=True,
        include_skipped=True,
        day_filter=day_key,
        reset_offset=True,
    )
    rebuilt = 0
    if memory_store is not None:
        try:
            rebuilt = int(memory_store.refresh_setup_stats_for_day(day_key))
        except Exception:
            pass
    result.detail = f"{result.detail}; day={day_key}; setup_stats_rebuilt={rebuilt}"
    return result
