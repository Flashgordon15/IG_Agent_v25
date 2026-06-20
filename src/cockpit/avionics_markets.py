"""Flight Deck Card B — per-asset market slices keyed GOLD / WALL_STREET / JAPAN_225 / EUR_USD."""

from __future__ import annotations

from typing import Any

EPIC_ASSET_KEYS: dict[str, str] = {
    "CS.D.CFPGOLD.CFP.IP": "GOLD",
    "IX.D.DOW.IFM.IP": "WALL_STREET",
    "IX.D.NIKKEI.IFM.IP": "JAPAN_225",
    "CS.D.EURUSD.CFD.IP": "EUR_USD",
}

# Flight Deck + stream aliases — EUR/USD hub rows may arrive as EURUSD (no underscore).
ASSET_KEY_ALIASES: dict[str, str] = {
    "EURUSD": "EUR_USD",
    "EUR/USD": "EUR_USD",
}

ASSET_EPIC_KEYS: dict[str, str] = {v: k for k, v in EPIC_ASSET_KEYS.items()}

CANONICAL_ASSET_KEYS: tuple[str, ...] = (
    "GOLD",
    "WALL_STREET",
    "JAPAN_225",
    "EUR_USD",
)


def _confidence_from_market_slice(slice_: dict[str, Any]) -> float | None:
    """Per-epic ML-blended confidence from signal_confidence gate (not global autopilot)."""
    health = slice_.get("health")
    if isinstance(health, dict):
        for gate in health.get("gates") or []:
            if not isinstance(gate, dict) or gate.get("name") != "signal_confidence":
                continue
            value = gate.get("value")
            if not isinstance(value, dict):
                continue
            blended = value.get("confidence")
            if blended is not None:
                try:
                    return float(blended)
                except (TypeError, ValueError):
                    pass
            nested = value.get("signal")
            if isinstance(nested, dict) and nested.get("confidence") is not None:
                try:
                    return float(nested["confidence"])
                except (TypeError, ValueError):
                    pass
            rules = value.get("rules_confidence")
            if rules is not None:
                try:
                    return float(rules)
                except (TypeError, ValueError):
                    pass
    signal = slice_.get("signal")
    if isinstance(signal, dict):
        for key in ("confidence", "rules_confidence", "signal_core_score"):
            raw = signal.get(key)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
    return None


def _live_rsi_for_epic(epic: str) -> float | None:
    """Read organic RSI from the trading loop signal engine (per epic)."""
    epic_key = str(epic or "").strip()
    if not epic_key:
        return None
    try:
        from api.agent_control import get_trading_loop

        bundle = get_trading_loop()
        if bundle is None or not hasattr(bundle, "loops"):
            return None
        for tl in bundle.loops:
            if str(getattr(tl, "_epic", "") or "").strip() != epic_key:
                continue
            se = getattr(tl, "_signal_engine", None)
            market = str(getattr(tl, "_market", "") or "").strip()
            if se is None or not market:
                return None
            snap = (getattr(se, "last_snapshot", None) or {}).get(market) or {}
            hud_rsi = snap.get("hud_rsi")
            if hud_rsi is not None:
                try:
                    return float(hud_rsi)
                except (TypeError, ValueError):
                    pass
            last = snap.get("last")
            if last is not None and hasattr(last, "get"):
                rsi = last.get("rsi")
                if rsi is not None:
                    return float(rsi)
    except Exception:
        pass
    return None


def _rsi_from_market_slice(slice_: dict[str, Any], *, epic: str = "") -> float | None:
    signal = slice_.get("signal")
    if isinstance(signal, dict):
        if signal.get("rsi") is not None:
            try:
                return float(signal["rsi"])
            except (TypeError, ValueError):
                pass
        snap = signal.get("snapshot")
        if isinstance(snap, dict):
            last = snap.get("last")
            if isinstance(last, dict) and last.get("rsi") is not None:
                try:
                    return float(last["rsi"])
                except (TypeError, ValueError):
                    pass
            if snap.get("rsi") is not None:
                try:
                    return float(snap["rsi"])
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
    live = _live_rsi_for_epic(epic or str(slice_.get("epic") or ""))
    if live is not None:
        return live
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


def _attach_hub_quote(epic: str, row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Merge REST-poll / hub quote fields when snapshot slice lacks live bid/offer."""
    out = dict(row)
    try:
        bid = float(out.get("bid") or 0)
        offer = float(out.get("offer") or 0)
    except (TypeError, ValueError):
        bid = offer = 0.0
    if bid > 0 and offer > 0:
        return out

    epics = payload.get("epics")
    if isinstance(epics, dict):
        quote = epics.get(epic)
        if isinstance(quote, dict):
            try:
                q_bid = float(quote.get("bid") or 0)
                q_offer = float(quote.get("offer") or 0)
            except (TypeError, ValueError):
                q_bid = q_offer = 0.0
            if q_bid > 0 and q_offer > 0:
                out["bid"] = q_bid
                out["offer"] = q_offer
                out["spread"] = quote.get("spread") or max(0.0, q_offer - q_bid)
                out["tick_age_s"] = quote.get("age_s") or quote.get("tick_age_s")

    spread_map = payload.get("spread")
    if isinstance(spread_map, dict):
        sp = spread_map.get(epic)
        if isinstance(sp, dict):
            if out.get("z_score") is None:
                out["z_score"] = sp.get("z_score")
            if out.get("throttle") is None:
                out["throttle"] = sp.get("throttle")
    return out


def _publish_asset_aliases(
    asset_markets: dict[str, Any],
    avionics_assets: dict[str, Any],
) -> None:
    """Mirror canonical asset rows under stream alias keys (e.g. EURUSD → EUR_USD)."""
    for alias, canonical in ASSET_KEY_ALIASES.items():
        if canonical in asset_markets:
            asset_markets[alias] = asset_markets[canonical]
        if canonical in avionics_assets:
            avionics_assets[alias] = avionics_assets[canonical]


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
            hub_only = _attach_hub_quote(epic, {"epic": epic}, out)
            if float(hub_only.get("bid") or 0) <= 0:
                continue
            slice_ = hub_only
        row = _attach_hub_quote(epic, dict(slice_), out)
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        confidence = _confidence_from_market_slice(row)
        rsi = _rsi_from_market_slice(row, epic=epic)
        if confidence is not None:
            row = {**row, "confidence": confidence, "signal_confidence": confidence}
        if rsi is not None:
            row = {**row, "rsi": rsi}
            if isinstance(signal, dict):
                signal = {**signal, "rsi": rsi}
                row["signal"] = signal
        asset_markets[asset_key] = row
        fitness_score = None
        health_block = row.get("health")
        if isinstance(health_block, dict):
            for gate in health_block.get("gates") or []:
                if not isinstance(gate, dict) or gate.get("name") != "environment_fitness":
                    continue
                val = gate.get("value")
                if isinstance(val, dict) and val.get("score") is not None:
                    fitness_score = val.get("score")
                    break
        avionics_assets[asset_key] = {
            "asset_key": asset_key,
            "epic": epic,
            "confidence": confidence,
            "signal_confidence": confidence,
            "direction": signal.get("direction"),
            "setup": signal.get("setup"),
            "block_reason": signal.get("block_reason"),
            "rsi": rsi,
            "signal": signal,
            "health": row.get("health"),
            "fitness": fitness_score,
            "points": row.get("points"),
            "trade_eligibility": row.get("trade_eligibility"),
        }

    _publish_asset_aliases(asset_markets, avionics_assets)

    if merged_epic or asset_markets:
        out["markets"] = {**merged_epic, **asset_markets}
    if asset_markets:
        out["hud_markets"] = asset_markets
    if avionics_assets:
        out["avionics_assets"] = avionics_assets
    try:
        from apex.avionics_story import snapshot_avionics_stories

        stories = snapshot_avionics_stories(limit=36)
        if stories:
            out["avionics_stories"] = stories
    except Exception:
        pass
    return out


def package_avionics_hud_broadcast(payload: dict[str, Any]) -> dict[str, Any]:
    """
    2.5 Hz WebSocket envelope — explicit per-asset HUD rows from snapshot_store.

    Top-level keys GOLD / WALL_STREET / JAPAN_225 / EUR_USD plus ``avionics_hud``
  mirror the same isolated metrics so Flight Deck cards cannot cross-read.
    """
    out = enrich_avionics_markets(payload)
    snapshot_markets = _snapshot_markets_by_epic()
    if snapshot_markets:
        out["snapshot_markets_epics"] = sorted(snapshot_markets.keys())

    hud_cards: dict[str, Any] = {}
    assets = out.get("avionics_assets")
    if not isinstance(assets, dict):
        assets = {}

    for asset_key in CANONICAL_ASSET_KEYS:
        epic = ASSET_EPIC_KEYS.get(asset_key, "")
        row = assets.get(asset_key)
        if not isinstance(row, dict) and epic:
            slice_ = snapshot_markets.get(epic)
            if isinstance(slice_, dict):
                row = {
                    "asset_key": asset_key,
                    "epic": epic,
                    "confidence": _confidence_from_market_slice(slice_),
                    "rsi": _rsi_from_market_slice(slice_, epic=epic),
                }
        if not isinstance(row, dict):
            continue
        card = {
            "asset_key": asset_key,
            "epic": row.get("epic") or epic,
            "confidence": row.get("confidence"),
            "signal_confidence": row.get("signal_confidence") or row.get("confidence"),
            "rsi": row.get("rsi"),
            "direction": row.get("direction"),
            "setup": row.get("setup"),
            "block_reason": row.get("block_reason"),
            "signal": row.get("signal") if isinstance(row.get("signal"), dict) else {},
        }
        hud_cards[asset_key] = card
        out[asset_key] = card

    if hud_cards:
        out["avionics_hud"] = hud_cards
    return out
