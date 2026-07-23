"""Map logical CFD epics to broker-valid instrument codes (spread bet vs CFD)."""

from __future__ import annotations

import os
from typing import Any

import threading
import time
from datetime import datetime, timezone

_WEEKEND_EPIC_MAP: dict[str, str] = {
    "IX.D.DOW.IFM.IP": "IX.D.SUNDOW.IFS.IP",
    "IX.D.FTSE.IFM.IP": "IX.D.SUNFUN.IFM.IP",
    "IX.D.DAX.IFM.IP": "IX.D.SUNDAX.IFS.IP",
    "IX.D.NIKKEI.IFM.IP": "IX.D.SUNNAS.IFS.IP",
    "CS.D.CRUDE.CFD.IP": "IX.D.SUNCL.UMP.IP",
}


def _is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5


def resolve_weekend_epic(epic: str) -> str:
    """Remap standard epic to IG weekend equivalent on Sat/Sun in DEMO mode."""
    if not _is_weekend():
        return epic
    try:
        from system.agent_execution_mode import demo_sandbox_unblock_active
        if not demo_sandbox_unblock_active():
            return epic
    except Exception:
        return epic
    return _WEEKEND_EPIC_MAP.get(epic, epic)


# Canonical CFD keys → IG spread-bet daily epics (UK DEMO/LIVE).
_CFD_TO_SPREADBET_TODAY: dict[str, str] = {
    "CS.D.EURUSD.CFD.IP": "CS.D.EURUSD.TODAY.IP",
    "CS.D.GBPUSD.CFD.IP": "CS.D.GBPUSD.TODAY.IP",
}

_CFD_TO_SPREADBET_DAILY: dict[str, str] = {
    "CS.D.EURUSD.CFD.IP": "CS.D.EURUSD.DAILY.IP",
    "CS.D.GBPUSD.CFD.IP": "CS.D.GBPUSD.DAILY.IP",
}

_SPREADBET_PRODUCTS = frozenset({"SPREADBET", "SPREAD_BET", "SB", "SPREADBETTING"})

_WIRE_EPIC_CACHE_TTL_SEC = 300.0
_wire_epic_cache: dict[str, tuple[float, str]] = {}
_wire_epic_cache_lock = threading.Lock()


def _wire_cache_key(logical_epic: str, product: str) -> str:
    return f"{logical_epic}|{product}"


def _cached_wire_epic(logical_epic: str, product: str) -> str | None:
    key = _wire_cache_key(logical_epic, product)
    now = time.time()
    with _wire_epic_cache_lock:
        row = _wire_epic_cache.get(key)
        if row is None:
            return None
        ts, wire = row
        if now - ts > _WIRE_EPIC_CACHE_TTL_SEC:
            _wire_epic_cache.pop(key, None)
            return None
        return wire


def _store_wire_epic(logical_epic: str, product: str, wire: str) -> None:
    with _wire_epic_cache_lock:
        _wire_epic_cache[_wire_cache_key(logical_epic, product)] = (
            time.time(),
            str(wire or "").strip(),
        )


def clear_wire_epic_cache() -> None:
    """Test / operator hook — drop cached wire epic probes."""
    with _wire_epic_cache_lock:
        _wire_epic_cache.clear()


def normalize_account_product(product: str | None) -> str:
    p = str(product or "").strip().upper()
    if p in _SPREADBET_PRODUCTS:
        return "SPREADBET"
    if p in ("CFD", "CFDS"):
        return "CFD"
    return p or "CFD"


def resolve_order_epic(epic: str, *, account_product: str | None = None) -> str:
    """
    Return the epic string IG expects on POST /positions/otc.

    Spread-betting accounts reject ``.CFD.IP`` — use ``.TODAY.IP`` (default) or
    ``.DAILY.IP`` when ``IG_SPREADBET_EPIC_SUFFIX=daily``.

    Prefer ``resolve_order_epic_safe`` at dispatch time — it probes GET /markets
    and falls back to ``.CFD.IP`` when spread-bet remaps return 403/404.
    """
    key = str(epic or "").strip()
    if not key:
        return key
    product = normalize_account_product(
        account_product or os.environ.get("IG_BROKER_ACCOUNT_PRODUCT", "")
    )
    if product != "SPREADBET":
        return key
    suffix_mode = str(os.environ.get("IG_SPREADBET_EPIC_SUFFIX", "today")).strip().lower()
    table = _CFD_TO_SPREADBET_DAILY if suffix_mode == "daily" else _CFD_TO_SPREADBET_TODAY
    if key in table:
        return table[key]
    if key.endswith(".CFD.IP"):
        replacement = ".DAILY.IP" if suffix_mode == "daily" else ".TODAY.IP"
        return key.replace(".CFD.IP", replacement)
    return key


def resolve_epic_list(epics: list[str] | tuple[str, ...], *, account_product: str | None = None) -> tuple[str, ...]:
    return tuple(resolve_order_epic(e, account_product=account_product) for e in epics)


def _logical_cfd_epic(epic: str) -> str:
    """Normalize spread-bet wire codes to canonical hub/OHLC keys."""
    key = str(epic or "").strip()
    if not key:
        return key
    if key.endswith(".TODAY.IP") or key.endswith(".DAILY.IP"):
        return key.replace(".TODAY.IP", ".CFD.IP").replace(".DAILY.IP", ".CFD.IP")
    return key


def _dual_core_cfg(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    if hasattr(cfg, "get"):
        try:
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict):
                return dual
        except Exception:
            pass
    dual = getattr(cfg, "dual_core", None) or {}
    return dual if isinstance(dual, dict) else {}


def _config_product_override(cfg: Any | None) -> str | None:
    if cfg is None:
        return None
    dual = _dual_core_cfg(cfg)
    candidates: list[Any] = []
    for key in ("broker_account_product", "account_product", "ig_account_product"):
        val = getattr(cfg, key, None)
        if val is None and hasattr(cfg, "get"):
            try:
                val = cfg.get(key)
            except Exception:
                val = None
        candidates.append(val)
    candidates.append(dual.get("broker_account_product"))
    for val in candidates:
        if val and str(val).strip().lower() not in ("auto", ""):
            return normalize_account_product(str(val))
    return None


def _account_type_from_row(acc: dict[str, Any]) -> str:
    return str(
        acc.get("accountType")
        or acc.get("account_type")
        or acc.get("productType")
        or ""
    ).upper()


def detect_account_product_from_rest(rest: Any | None) -> str:
    """Read accountType from login session or GET /accounts."""
    if rest is None:
        return "CFD"
    auth = getattr(rest, "_auth", None)
    tokens = getattr(auth, "tokens", None) if auth else None
    raw = getattr(tokens, "raw", None) or {}
    account_id = str(
        getattr(tokens, "account_id", "")
        or getattr(rest, "account_id", "")
        or raw.get("currentAccountId")
        or raw.get("accountId")
        or ""
    )

    def _pick(accounts: list[dict[str, Any]]) -> str | None:
        if not accounts:
            return None
        if account_id:
            for acc in accounts:
                if str(acc.get("accountId", "")) == account_id:
                    at = _account_type_from_row(acc)
                    if at:
                        return normalize_account_product(at)
        spread = [
            acc for acc in accounts if normalize_account_product(_account_type_from_row(acc)) == "SPREADBET"
        ]
        if len(spread) == 1:
            return "SPREADBET"
        for acc in accounts:
            if acc.get("preferred") or acc.get("isPrimary"):
                at = _account_type_from_row(acc)
                if at:
                    return normalize_account_product(at)
        at = _account_type_from_row(accounts[0])
        return normalize_account_product(at) if at else None

    picked = _pick(list(raw.get("accounts") or []))
    if picked:
        rest._account_product_type = picked  # type: ignore[attr-defined]
        return picked

    cached = getattr(rest, "_account_product_type", None)
    if cached:
        return normalize_account_product(str(cached))
    try:
        resp = rest.request("GET", "/accounts", headers=rest._auth_headers("1"), timeout=6)
        if resp.status_code == 200:
            picked = _pick(list(resp.json().get("accounts") or []))
            if picked:
                rest._account_product_type = picked  # type: ignore[attr-defined]
                return picked
    except Exception:
        pass
    return "CFD"


def resolve_account_product(*, rest: Any | None = None, cfg: Any | None = None) -> str:
    """Config override (incl. dual_core) → env → live REST probe → CFD default."""
    override = _config_product_override(cfg)
    if override:
        return override
    env = os.environ.get("IG_BROKER_ACCOUNT_PRODUCT", "").strip()
    if env and env.lower() != "auto":
        return normalize_account_product(env)
    if rest is not None:
        return detect_account_product_from_rest(rest)
    return "CFD"


def resolve_hot_path_epics_from_config(cfg: Any | None = None, *, rest: Any | None = None) -> tuple[str, ...]:
    """
    Logical hub epics for hot-path stack (always canonical ``.CFD.IP`` keys).

    Wire epics for POST /positions/otc are resolved at dispatch via ``resolve_order_epic``.
    """
    _ = rest  # reserved for future account-aware validation/logging
    dual = _dual_core_cfg(cfg)
    fallback = dual.get("hot_path_epics_cfd_fallback") or []
    if isinstance(fallback, (list, tuple)) and fallback:
        return tuple(_logical_cfd_epic(str(e)) for e in fallback if e)
    raw = dual.get("hot_path_epics") or []
    epics = [_logical_cfd_epic(str(e)) for e in raw if e] if isinstance(raw, (list, tuple)) else []
    if not epics:
        epics = ["CS.D.EURUSD.CFD.IP", "CS.D.GBPUSD.CFD.IP"]
    return tuple(dict.fromkeys(epics))


def _is_fx_logical_epic(epic: str) -> bool:
    key = _logical_cfd_epic(str(epic or "").strip())
    return key in _CFD_TO_SPREADBET_TODAY or key.endswith(".CFD.IP") and key.startswith("CS.D.")


def _spreadbet_wire_candidates(logical_epic: str, *, account_product: str) -> list[str]:
    """Candidate wire epics for FX — spread-bet remaps first, CFD fallback last."""
    if normalize_account_product(account_product) != "SPREADBET":
        return [logical_epic]
    cfd = _logical_cfd_epic(logical_epic)
    candidates: list[str] = []
    suffix_mode = str(os.environ.get("IG_SPREADBET_EPIC_SUFFIX", "today")).strip().lower()
    table = _CFD_TO_SPREADBET_DAILY if suffix_mode == "daily" else _CFD_TO_SPREADBET_TODAY
    mapped = table.get(cfd, "")
    if mapped:
        candidates.append(mapped)
    if cfd.endswith(".CFD.IP"):
        for suffix in (".TODAY.IP", ".DAILY.IP"):
            alt = cfd.replace(".CFD.IP", suffix)
            if alt not in candidates:
                candidates.append(alt)
    if cfd not in candidates:
        candidates.append(cfd)
    if logical_epic not in candidates and logical_epic != cfd:
        candidates.insert(0, logical_epic)
    return candidates


def resolve_order_epic_safe(
    rest: Any | None,
    epic: str,
    *,
    cfg: Any | None = None,
) -> str:
    """
    Pick a wire epic that returns HTTP 200 from GET /markets/{epic}.

    For spread-bet FX: probe TODAY/DAILY remaps; keep CFD.IP when account lacks
    FX_BET_ALL access (403 on spread-bet codes).
    """
    key = str(epic or "").strip()
    if not key:
        return key
    key = resolve_weekend_epic(key)
    product = resolve_account_product(rest=rest, cfg=cfg)
    if rest is None:
        return resolve_order_epic(key, account_product=product)

    cached = _cached_wire_epic(key, product)
    if cached:
        return cached

    # Non-FX: wire epic is deterministic — avoid REST probe storms during rotation sweeps.
    if not _is_fx_logical_epic(key):
        wire = resolve_order_epic(key, account_product=product)
        _store_wire_epic(key, product, wire)
        return wire

    candidates = _spreadbet_wire_candidates(key, account_product=product)
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            rest.ensure_session()
            resp = rest.request(
                "GET",
                f"/markets/{candidate}",
                headers=rest._auth_headers("3"),
                timeout=6,
                budget_priority=True,
            )
            if resp.status_code == 401:
                rest.login()
                resp = rest.request(
                    "GET",
                    f"/markets/{candidate}",
                    headers=rest._auth_headers("3"),
                    timeout=6,
                    budget_priority=True,
                )
            if resp.status_code == 200:
                _store_wire_epic(key, product, candidate)
                return candidate
        except Exception:
            continue
    wire = resolve_order_epic(key, account_product=product)
    _store_wire_epic(key, product, wire)
    return wire
