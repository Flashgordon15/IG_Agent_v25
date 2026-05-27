"""CSV decision journal."""

from __future__ import annotations

import csv
import os
from datetime import datetime

from data.models import Quote


class DecisionJournal:
    FIELDNAMES = [
        "time", "market", "epic", "signal", "price",
        "raw_confidence", "adjusted_confidence", "learning_delta",
        "setup_key", "action", "deal_reference", "notes",
    ]

    def __init__(self, path: str) -> None:
        self.path = path
        self.ensure_exists()

    def ensure_exists(self) -> None:
        if not os.path.exists(self.path):
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.FIELDNAMES).writeheader()

    def write(
        self,
        *,
        market: str,
        epic: str,
        signal: str,
        quote: Quote | float,
        raw_confidence: float,
        adjusted_confidence: float,
        learning_delta: float,
        setup_key: str,
        action: str,
        deal_reference: str = "",
        notes: str = "",
    ) -> None:
        price = quote.mid if isinstance(quote, Quote) else float(quote)
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            writer.writerow(
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "market": market,
                    "epic": epic,
                    "signal": signal,
                    "price": price,
                    "raw_confidence": round(float(raw_confidence), 1),
                    "adjusted_confidence": round(float(adjusted_confidence), 1),
                    "learning_delta": learning_delta,
                    "setup_key": setup_key,
                    "action": action,
                    "deal_reference": deal_reference,
                    "notes": notes.replace("\n", " | "),
                }
            )

    def is_writable(self) -> bool:
        import os
        from pathlib import Path
        p = Path(self.path)
        parent = p.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
        if p.exists():
            return os.access(p, os.W_OK)
        return os.access(parent, os.W_OK)
