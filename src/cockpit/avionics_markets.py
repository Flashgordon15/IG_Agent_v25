"""Flight Deck Card B — per-asset market slices keyed GOLD / WALL_STREET / JAPAN_225 / EUR_USD."""

from __future__ import annotations

from typing import Any

EPIC_ASSET_KEYS: dict[str, str] = {
    "CS.D.CFPGOLD.CFP.IP": "GOLD",
    "IX.D.DOW.IFM.IP": "WALL_STREET",
    "IX.D.NIKKEI.IFM.IP": "JAPAN_225",
    "CS.D.EURUSD.CFD.IP": "EUR_USD",
}

ASSET_EPIC_KEYS: dict[str, str] = {v: k for k, v in EPIC_ASSET_KEYS.items()}


def _rsi_from_market_slice(slice_: dict[str, Any]) -> float | None:
    signal = slice_.get("signal")
    if isinstance(signal, dict) and signal.get("rsi") is not None:
        try:
            return float(signal["rsi"])
        except (TypeError, ValueError):
            pass
    health = slice_.get("health")
    if not isinstance(health, dict):
        return None
    for gate in health.get("gates") or []:
        if not isinstance(gate, dict) or gate.get("name") != "signal_confidence":
            continue
        value = gate.get("value")
        if not isinstance(value, dict):
            continue
        inner = value.get("signal")
        if isinstance(inner, dict):
            snap = inner.get("snapshot")
            if isinstance(snap, dict) and snap.get("rsi") is not None:
                try:
                    return float(snap["rsi"])
                except (TypeError, ValueError):
                    pass
    return None


def _snapshot_markets_by_epic() -> dict[str, dict[str, Any]]:
    """Read orchestrator market slices from the dashboard snapshot bus."""
    try:
        from api.snapshot_store import get_tick

        tick = get_tick() or {}
    except Exception:
        return {}
    raw = tick.get("markets")
    if isinstance(raw, dict) and raw:
        return {
            str(epic): dict(row)
            for epic, row in raw.items()
            if epic and isinstance(row, dict)
        }
    epic = str(tick.get("epic") or "").strip()
    if epic:
        return {epic: dict(tick)}
    return {}


def enrich_avionics_markets(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Attach asset-keyed market telemetry for isolated Card B gauges.

    ``markets`` contains both epic keys and asset keys (GOLD, WALL_STREET, …).
    ``avionics_assets`` / ``hud_markets`` mirror the asset-key view for JS readers.
    """
    out = dict(payload)
    snapshot_markets = _snapshot_markets_by_epic()

    existing = out.get("markets")
    merged_epic: dict[str, Any] = (
        {str(k): dict(v) for k, v in existing.items() if isinstance(v, dict)}
        if isinstance(existing, dict)
        else {}
    )
    for epic, row in snapshot_markets.items():
        merged_epic[epic] = row

    asset_markets: dict[str, Any] = {}
    avionics_assets: dict[str, Any] = {}
    for epic, asset_key in EPIC_ASSET_KEYS.items():
        slice_ = merged_epic.get(epic)
        if not isinstance(slice_, dict):
            continue
        row = dict(slice_)
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        rsi = _rsi_from_market_slice(row)
        asset_markets[asset_key] = row
        avionics_assets[asset_key] = {
            "asset_key": asset_key,
            "epic": epic,
            "confidence": signal.get("confidence"),
            "signal_confidence": signal.get("confidence"),
            "direction": signal.get("direction"),
            "setup": signal.get("setup"),
            "block_reason": signal.get("block_reason"),
            "rsi": rsi,
            "signal": signal,
            "health": row.get("health"),
            "points": row.get("points"),
            "trade_eligibility": row.get("trade_eligibility"),
        }
        if rsi is not None:
            asset_markets[asset_key] = {**row, "rsi": rsi}

    if merged_epic or asset_markets:
        out["markets"] = {**merged_epic, **asset_markets}
    if asset_markets:
        out["hud_markets"] = asset_markets
    if avionics_assets:
        out["avionics_assets"] = avionics_assets
    return out
