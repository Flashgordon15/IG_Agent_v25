"""
Broker price precision — preserve IG fractional levels (5dp FX, no int casts).
"""

from __future__ import annotations

from typing import Any

from system.pnl_math import pip_size_for_epic

_PLACEHOLDER_TOKENS = frozenset(
    {"—", "-", "n/a", "na", "null", "none", "mock", "placeholder", ""}
)


class PricePrecisionError(ValueError):
    """Raised when a price field cannot be parsed as a broker float."""


def is_placeholder_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    return text in _PLACEHOLDER_TOKENS


def decimal_places_for_epic(epic: str) -> int:
    if pip_size_for_epic(epic) is not None:
        return 5
    epic_u = str(epic or "").upper()
    if "GOLD" in epic_u or "CFPGOLD" in epic_u:
        return 2
    if "EUR" in epic_u or "GBP" in epic_u or "USD" in epic_u:
        return 5
    return 5


def parse_broker_price(value: Any, *, epic: str = "") -> float:
    """
    Parse IG price fields as high-precision float — never integer-truncate.

    Raises PricePrecisionError on placeholders or invalid values.
    """
    if is_placeholder_value(value):
        raise PricePrecisionError(f"placeholder price rejected: {value!r}")
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise PricePrecisionError(f"invalid price {value!r}") from exc
    if price <= 0:
        raise PricePrecisionError(f"non-positive price {price!r}")
    # Preserve broker fractions — round only to instrument precision for storage
    places = decimal_places_for_epic(epic)
    return round(price, places)


def parse_broker_price_optional(value: Any, *, epic: str = "") -> float | None:
    if value is None or is_placeholder_value(value):
        return None
    try:
        return parse_broker_price(value, epic=epic)
    except PricePrecisionError:
        return None


def coerce_price_fields(row: dict[str, Any], *, epic: str = "") -> dict[str, Any]:
    """Normalize level/entry/current/stop fields in a position row."""
    out = dict(row)
    ep = str(out.get("epic") or epic or "")
    for key in ("entry", "level", "open_level", "current", "mark", "stop", "stop_level", "bid", "offer"):
        if key not in out or out[key] is None:
            continue
        parsed = parse_broker_price_optional(out[key], epic=ep)
        if parsed is not None:
            out[key] = parsed
    return out
