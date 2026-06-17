"""
High-frequency telemetry packet generator with byte-level integrity tracking.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from cockpit.telemetry_schema import (
    TelemetrySchemaMismatchError,
    validate_position_payload,
)
from system.price_precision import is_placeholder_value


class CapacityIntegrityError(Exception):
    """Fatal validation — dropped frame, duplicate seq, or schema drift."""


@dataclass
class SchemaDriftTracker:
    """Tracks sequence integrity across a flood run."""

    seen_deal_ids: set[str] = field(default_factory=set)
    seen_seq: set[int] = field(default_factory=set)
    bytes_in: int = 0
    frames_ok: int = 0
    last_normalized: dict[str, Any] | None = None

    def ingest(self, raw: dict[str, Any], *, seq: int) -> dict[str, Any]:
        if seq in self.seen_seq:
            raise CapacityIntegrityError(f"duplicate sequence {seq}")
        self.seen_seq.add(seq)

        payload_bytes = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
        if not payload_bytes:
            raise CapacityIntegrityError("empty payload bytes")
        self.bytes_in += len(payload_bytes)

        normalized = validate_position_payload(raw)
        deal_id = str(normalized.get("dealId") or "")
        if deal_id in self.seen_deal_ids:
            raise CapacityIntegrityError(f"duplicate dealId {deal_id}")
        self.seen_deal_ids.add(deal_id)

        for key in ("entry", "level", "profitAndLoss"):
            val = normalized.get(key)
            if val is not None and is_placeholder_value(val):
                raise CapacityIntegrityError(f"placeholder {key}={val!r}")

        if self.last_normalized is not None:
            for key in ("entry", "level", "profitAndLoss"):
                prev = self.last_normalized.get(key)
                curr = normalized.get(key)
                if prev is not None and curr is not None and key in raw:
                    if abs(float(curr) - float(prev)) > 1e-9 and raw.get(key) == prev:
                        raise CapacityIntegrityError(
                            f"schema drift: {key} mutated without payload change"
                        )

        self.last_normalized = dict(normalized)
        self.frames_ok += 1
        return normalized


class TelemetryPacketGenerator:
    """Synthesizes IG-position JSON at configurable rate for stress harness."""

    def __init__(self, *, epic: str = "CS.D.EURUSD.CFD.IP") -> None:
        self._epic = epic
        self._base_entry = 1.16032
        self._seq = 0

    def next_payload(self, *, pnl_delta: float = 0.0) -> tuple[int, dict[str, Any]]:
        self._seq += 1
        entry = round(self._base_entry, 5)
        current = round(entry + 0.00015 * (self._seq % 5), 5)
        pnl = round(-12.0 + pnl_delta + self._seq * 0.01, 2)
        deal_id = f"STRESS-{self._seq:08d}"
        raw = {
            "dealId": deal_id,
            "epic": self._epic,
            "side": "BUY",
            "level": entry,
            "entry": entry,
            "current": current,
            "stop": round(entry - 0.00080, 5),
            "profitAndLoss": pnl,
            "currency": "GBP",
            "size": 1.5,
            "ts": time.time(),
        }
        return self._seq, raw

    def telemetry_frame(self, *, seq: int | None = None) -> dict[str, Any]:
        """Full cockpit snapshot frame for queue/WebSocket flood."""
        s, pos = self.next_payload()
        if seq is not None:
            s = seq
        return {
            "type": "TELEMETRY_FRAME",
            "seq": s,
            "ts": time.time(),
            "position_map": {pos["dealId"]: pos},
            "epics": {self._epic: {"bid": pos["current"], "offer": pos["current"] + 0.00002}},
            "gates": {"G1": True, "G2": True, "G3": True, "G4": True},
        }
