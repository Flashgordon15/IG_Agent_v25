"""Parse IG monetary strings (e.g. profitAndLoss from transaction history)."""

from __future__ import annotations

import re


def parse_ig_money(value: object) -> float | None:
    """Parse '£12.50', '-12.5', 12.5 -> float; None if unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = s.strip("()")
    s = re.sub(r"[£$€,\s]", "", s)
    if s.startswith("-"):
        negative = True
        s = s[1:]
    if not s:
        return None
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return None
