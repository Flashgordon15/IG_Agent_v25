"""Map logical CFD epics to broker-valid instrument codes (spread bet vs CFD)."""

from __future__ import annotations

import os
from typing import Any

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


def detect_account_product_from_rest(rest: Any | None) -> str:
    """Read accountType from login session or GET /accounts."""
    if rest is None:
        return "CFD"
    auth = getattr(rest, "_auth", None)
    tokens = getattr(auth, "tokens", None) if auth else None
    raw = getattr(tokens, "raw", None) or {}
    account_id = str(getattr(tokens, "account_id", "") or getattr(rest, "account_id", "") or "")
    for acc in raw.get("accounts") or []:
        if account_id and str(acc.get("accountId", "")) != account_id:
            continue
        at = str(acc.get("accountType") or acc.get("account_type") or "").upper()
        if at:
            return normalize_account_product(at)
    cached = getattr(rest, "_account_product_type", None)
    if cached:
        return normalize_account_product(str(cached))
    try:
        resp = rest.request("GET", "/accounts", headers=rest._auth_headers("1"), timeout=6)
        if resp.status_code == 200:
            for acc in resp.json().get("accounts") or []:
                if account_id and str(acc.get("accountId", "")) != account_id:
                    continue
                at = str(acc.get("accountType") or "").upper()
                if at:
                    rest._account_product_type = at  # type: ignore[attr-defined]
                    return normalize_account_product(at)
    except Exception:
        pass
    return "CFD"


def resolve_account_product(*, rest: Any | None = None, cfg: Any | None = None) -> str:
    """Config override → env → live REST probe → CFD default."""
    if cfg is not None:
        for key in ("broker_account_product", "account_product", "ig_account_product"):
            val = getattr(cfg, key, None)
            if val is None and hasattr(cfg, "get"):
                try:
                    val = cfg.get(key)
                except Exception:
                    val = None
            if val and str(val).strip().lower() not in ("auto", ""):
                return normalize_account_product(str(val))
    env = os.environ.get("IG_BROKER_ACCOUNT_PRODUCT", "").strip()
    if env and env.lower() != "auto":
        return normalize_account_product(env)
    if rest is not None:
        return detect_account_product_from_rest(rest)
    return "CFD"


def resolve_hot_path_epics_from_config(cfg: Any | None = None, *, rest: Any | None = None) -> tuple[str, ...]:
    """hot_path_epics from config, broker-resolved for spread bet vs CFD."""
    dual: dict[str, Any] = {}
    if cfg is not None:
        dual = getattr(cfg, "dual_core", None) or {}
        if not isinstance(dual, dict) and hasattr(cfg, "get"):
            try:
                dual = cfg.get("dual_core") or {}
            except Exception:
                dual = {}
    product = resolve_account_product(cfg=cfg, rest=rest)
    if product == "CFD":
        fallback = dual.get("hot_path_epics_cfd_fallback") or []
        if isinstance(fallback, (list, tuple)) and fallback:
            return tuple(str(e) for e in fallback if e)
    raw = dual.get("hot_path_epics") or []
    epics = [str(e) for e in raw if e] if isinstance(raw, (list, tuple)) else []
    if not epics:
        epics = ["CS.D.EURUSD.CFD.IP", "CS.D.GBPUSD.CFD.IP"]
    return resolve_epic_list(epics, account_product=product)
