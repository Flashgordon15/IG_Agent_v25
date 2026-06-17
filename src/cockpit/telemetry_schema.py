"""
Live IG telemetry schema contract — strict validation at cockpit ingress.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from system.price_precision import (
    PricePrecisionError,
    is_placeholder_value,
    parse_broker_price,
    parse_broker_price_optional,
)


class TelemetrySchemaMismatchError(Exception):
    """Raised when broker telemetry drifts from the live IG schema contract."""

    def __init__(self, message: str, payload: Any) -> None:
        dump = json.dumps(payload, default=str, indent=2)
        super().__init__(f"{message}\n--- payload dump ---\n{dump}")
        self.payload_dump = dump


class IgPositionTelemetry(BaseModel):
    """Validated open-position row from IG REST / sync pipeline."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    dealId: str = Field(min_length=1)
    deal_id: str | None = None
    epic: str = ""
    side: str = ""
    direction: str = ""
    level: float | None = None
    entry: float | None = None
    current: float | None = None
    stop: float | None = None
    stop_level: float | None = None
    profitAndLoss: float | None = None
    profit_and_loss: float | None = None
    upl: float | None = None
    pnl_currency: float | None = None
    currency: str = "GBP"
    size: float = Field(gt=0, default=0.01)

    @field_validator("dealId", mode="before")
    @classmethod
    def _deal_id_required(cls, value: Any) -> str:
        text = str(value or "").strip()
        if is_placeholder_value(text):
            raise ValueError("dealId placeholder rejected")
        return text

    @field_validator("level", "entry", "current", "stop", "stop_level", mode="before")
    @classmethod
    def _price_fields(cls, value: Any, info: Any) -> float | None:
        if value is None or is_placeholder_value(value):
            return None
        epic = ""
        try:
            epic = str(getattr(info, "data", {}).get("epic") or "")
        except Exception:
            pass
        try:
            return parse_broker_price(value, epic=epic)
        except PricePrecisionError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator(
        "profitAndLoss",
        "profit_and_loss",
        "upl",
        "pnl_currency",
        mode="before",
    )
    @classmethod
    def _pnl_fields(cls, value: Any) -> float | None:
        if value is None or is_placeholder_value(value):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        from system.ig_money import parse_ig_money

        parsed = parse_ig_money(value)
        if parsed is None:
            raise ValueError(f"invalid broker P&L {value!r}")
        return float(parsed)

    def normalized_dict(self) -> dict[str, Any]:
        """Canonical dict for dashboards — dealId-keyed, broker-precision prices."""
        epic = str(self.epic or "")
        entry = self.entry
        if entry is None and self.level is not None:
            entry = self.level
        if entry is not None:
            entry = parse_broker_price(entry, epic=epic)
        current = self.current
        if current is not None:
            current = parse_broker_price(current, epic=epic)
        stop = self.stop if self.stop is not None else self.stop_level
        if stop is not None:
            stop = parse_broker_price_optional(stop, epic=epic)
        pnl = (
            self.profitAndLoss
            if self.profitAndLoss is not None
            else self.profit_and_loss
            if self.profit_and_loss is not None
            else self.upl
            if self.upl is not None
            else self.pnl_currency
        )
        side = str(self.side or self.direction or "").upper()
        deal_id = str(self.dealId or self.deal_id or "")
        return {
            "dealId": deal_id,
            "deal_id": deal_id,
            "epic": epic,
            "side": side,
            "direction": side,
            "entry": entry,
            "level": entry,
            "current": current,
            "stop": stop,
            "profitAndLoss": pnl,
            "pnl_currency": pnl,
            "upl": pnl,
            "currency": str(self.currency or "GBP").upper(),
            "size": float(self.size),
            "pnl_source": "ig_broker",
        }


def validate_position_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one IG position row; raises on schema drift."""
    if not isinstance(raw, dict):
        raise TelemetrySchemaMismatchError("position payload must be a dict", raw)
    try:
        model = IgPositionTelemetry.model_validate(raw)
        return model.normalized_dict()
    except Exception as exc:
        raise TelemetrySchemaMismatchError(
            f"IG position schema mismatch: {type(exc).__name__}: {exc}",
            raw,
        ) from exc


def validate_position_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build dealId-keyed position map with schema enforcement."""
    position_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TelemetrySchemaMismatchError("position row must be dict", row)
        normalized = validate_position_payload(row)
        deal_id = str(normalized.get("dealId") or normalized.get("deal_id") or "")
        if not deal_id:
            raise TelemetrySchemaMismatchError("dealId missing after normalize", row)
        position_map[deal_id] = normalized
    return position_map


class OrderBookLevel(BaseModel):
    """Single Level-2 depth row — price and displayed size."""

    model_config = ConfigDict(extra="forbid")

    price: float = Field(gt=0)
    size: float = Field(ge=0)


class OrderBookDepthPayload(BaseModel):
    """Level-2 order book depth contract for cockpit telemetry ingress."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    epic: str = Field(min_length=1)
    ts: float = Field(ge=0)
    bid_levels: list[OrderBookLevel] = Field(default_factory=list)
    ask_levels: list[OrderBookLevel] = Field(default_factory=list)
    source: str = "hub"

    def total_bid_size(self) -> float:
        return sum(float(level.size) for level in self.bid_levels)

    def total_ask_size(self) -> float:
        return sum(float(level.size) for level in self.ask_levels)

    def obi_ratio(self) -> float:
        from intelligence.order_book_imbalance import compute_obi_ratio

        return compute_obi_ratio(self)

    def institutional_flag(self, *, threshold: float = 0.65) -> str:
        from intelligence.order_book_imbalance import obi_institutional_flag

        return obi_institutional_flag(self.obi_ratio(), threshold=threshold)

    def normalized_dict(self) -> dict[str, Any]:
        ratio = self.obi_ratio()
        return {
            "epic": self.epic,
            "ts": self.ts,
            "bid_levels": [lv.model_dump() for lv in self.bid_levels],
            "ask_levels": [lv.model_dump() for lv in self.ask_levels],
            "bid_depth": self.total_bid_size(),
            "ask_depth": self.total_ask_size(),
            "obi_ratio": round(ratio, 4),
            "institutional_flag": self.institutional_flag(),
            "source": self.source,
        }


def validate_order_book_depth(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate Level-2 depth payload; raises on schema drift."""
    if not isinstance(raw, dict):
        raise TelemetrySchemaMismatchError("order book payload must be a dict", raw)
    try:
        model = OrderBookDepthPayload.model_validate(raw)
        return model.normalized_dict()
    except Exception as exc:
        raise TelemetrySchemaMismatchError(
            f"OrderBookDepthPayload schema mismatch: {type(exc).__name__}: {exc}",
            raw,
        ) from exc
