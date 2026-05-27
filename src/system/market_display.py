"""Short market labels for GUI display (aligned with IG Trading)."""

from __future__ import annotations

import re
from typing import Any

_EPIC_PREFIX_RE = re.compile(r"^IX\.", re.I)
_CASH_SUFFIX_RE = re.compile(r"^(.+?)\s+Cash\b", re.I)
_PARENS_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def format_market_display_name(
    market: str = "",
    *,
    epic: str = "",
    fallback: str = "",
) -> str:
    """
    Normalise IG instrument names to short labels (e.g. Japan 225).

    Examples:
      Japan 225 Cash ($1) -> Japan 225
      IX.D.NIKKEI.IFM.IP   -> Japan 225 (from config)
    """
    raw = str(market or "").strip()
    if raw:
        m = _CASH_SUFFIX_RE.match(raw)
        if m:
            return m.group(1).strip()[:24]
        trimmed = _PARENS_SUFFIX_RE.sub("", raw).strip()
        if trimmed and not _EPIC_PREFIX_RE.match(trimmed):
            return trimmed[:24]

    ep = str(epic or "").strip()
    if ep:
        mapped = _epic_to_label(ep)
        if mapped:
            return mapped[:24]

    fb = str(fallback or "").strip()
    if fb and not _EPIC_PREFIX_RE.match(fb):
        return fb[:24]

    if raw and not _EPIC_PREFIX_RE.match(raw):
        return raw[:24]
    if ep:
        return ep[:24]
    return "Market"


def format_market_from_row(row: dict[str, Any]) -> str:
    """Display label from a trade/position row dict."""
    return format_market_display_name(
        str(row.get("market") or ""),
        epic=str(row.get("epic") or ""),
        fallback=str(row.get("market") or ""),
    )


def configured_market_label(*, epic: str = "") -> str:
    """Primary configured market name for quote headers and status."""
    try:
        from system.config_loader import get_config

        cfg = get_config()
        ep = epic or str(cfg.epic or "")
        if ep:
            mapped = _epic_to_label(ep)
            if mapped:
                return mapped
        name = str(cfg.market_search or "").strip()
        if name:
            return name
    except Exception:
        pass
    return format_market_display_name(epic=epic)


def _epic_to_label(epic: str) -> str:
    ep = str(epic or "").strip()
    if not ep:
        return ""
    try:
        from system.config_loader import get_config

        cfg = get_config()
        if ep == str(cfg.epic or ""):
            name = str(cfg.market_search or "").strip()
            if name:
                return name
        for item in cfg.get("markets") or []:
            if isinstance(item, dict) and str(item.get("epic") or "") == ep:
                name = str(item.get("name") or item.get("market") or "").strip()
                if name:
                    return format_market_display_name(name, epic=ep)
    except Exception:
        pass
    return ""
