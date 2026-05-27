"""
IG transaction history helpers — date formats, parsing, display rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from system.ig_money import parse_ig_money
from system.market_display import format_market_display_name
from system.pnl_math import classify_result


def ig_date_range_dd_mm_yyyy(*, days_back: int = 2) -> tuple[str, str]:
    """Path dates for GET /history/transactions/{type}/{from}/{to} (dd-mm-yyyy)."""
    end = datetime.now()
    start = end - timedelta(days=max(1, days_back))
    return start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")


def coerce_to_ig_path_date(value: str) -> str:
    """Accept dd-mm-yyyy or ISO-ish strings; return dd-mm-yyyy for the IG path."""
    s = str(value).strip()
    if not s:
        return datetime.now().strftime("%d-%m-%Y")
    for fmt in ("%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            part = s[:19] if "T" in fmt else s[:10]
            return datetime.strptime(part.replace("T", " "), fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return s


def parse_ig_transaction_datetime(raw: str) -> str:
    """Normalise IG date (dd-MMM-yyyy, dd/mm/yy, or ISO) to YYYY-MM-DD HH:MM:SS for UI."""
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%Y-%m-%d",
    ):
        try:
            if "%H" in fmt:
                part = s[:19].replace("T", " ")
            else:
                part = s[:10]
            return datetime.strptime(part, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    if " " in s:
        return s[:19].replace("T", " ")
    return s[:10]


def _raw_has_clock_time(raw: str) -> bool:
    s = str(raw or "").strip()
    if not s or (":" not in s and "T" not in s):
        return False
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
    ):
        try:
            part = s[:19].replace("T", " ")
            dt = datetime.strptime(part, fmt)
            return not (dt.hour == 0 and dt.minute == 0 and dt.second == 0)
        except ValueError:
            continue
    return True


def _parse_ig_utc_datetime(raw: str) -> datetime | None:
    """Parse IG dateUtc (usually YYYY/MM/DD HH:MM:SS in UTC)."""
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y:%m:%d-%H:%M:%S",
    ):
        try:
            part = s[:19].replace("T", " ")
            dt = datetime.strptime(part, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def ig_deal_key_variants(key: str) -> set[str]:
    """
    Alternate lookup keys linking IG transaction `reference` (e.g. K4R6GHAD)
    to activity `dealId` (e.g. DIAAAAXK4R6GHAD).
    """
    k = str(key or "").strip().upper()
    if not k:
        return set()
    out = {k}
    if k.startswith("DIAAAA") and len(k) > 7:
        out.add(k[7:])
    elif len(k) >= 6 and not k.startswith("SIM"):
        # Short position suffix from /history/transactions
        out.add(f"DIAAAA{k}")
    return out


def _activity_closed_suffix(act: dict[str, Any]) -> str:
    """Parse 'Position/s closed: K4SSH5AA' from IG activity result."""
    result = str(act.get("result") or "")
    marker = "closed:"
    idx = result.lower().find(marker)
    if idx < 0:
        return ""
    tail = result[idx + len(marker) :].strip()
    return tail.split()[0].strip().upper() if tail else ""


def _activity_item_closed_at(act: dict[str, Any]) -> str:
    """Build a normalised closed_at from IG activity date + time (local)."""
    date_s = str(act.get("date") or "").strip()
    time_s = str(act.get("time") or "").strip()
    if not date_s or not time_s:
        return ""
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(date_s, fmt)
            hh, mm = time_s.split(":")[:2]
            return dt.strftime("%Y-%m-%d") + f" {hh}:{mm}:00"
        except ValueError:
            continue
    return ""


def build_activity_time_lookup(activities: list[dict[str, Any]]) -> dict[str, str]:
    """Map IG dealId / dealReference -> local closed_at string from /history/activity rows."""
    lookup: dict[str, str] = {}
    for act in activities:
        ts = _activity_item_closed_at(act)
        if not ts or not _raw_has_clock_time(ts):
            continue
        keys: set[str] = set()
        for key in (act.get("dealId"), act.get("dealReference"), act.get("reference")):
            keys.update(ig_deal_key_variants(str(key or "")))
        closed_suffix = _activity_closed_suffix(act)
        if closed_suffix:
            keys.update(ig_deal_key_variants(closed_suffix))
        for val in keys:
            if val:
                lookup[val] = ts
    return lookup


def lookup_activity_time(
    activity_times: dict[str, str] | None,
    *,
    deal_id: str = "",
    deal_reference: str = "",
) -> str:
    """Resolve activity timestamp using IG reference / dealId variants."""
    if not activity_times:
        return ""
    candidates: set[str] = set()
    for key in (deal_id, deal_reference):
        candidates.update(ig_deal_key_variants(key))
    for key in candidates:
        ts = activity_times.get(key, "")
        if ts:
            return ts
    return ""


def extract_ig_transaction_closed_at(
    txn: dict[str, Any],
    *,
    activity_time: str = "",
) -> str:
    """Best-effort local close timestamp matching IG Trading history."""
    if activity_time and _raw_has_clock_time(activity_time):
        return parse_ig_transaction_datetime(activity_time)

    date_utc = str(txn.get("dateUtc") or "").strip()
    if date_utc:
        utc_dt = _parse_ig_utc_datetime(date_utc)
        if utc_dt is not None:
            return utc_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        parsed = parse_ig_transaction_datetime(date_utc)
        if parsed and _raw_has_clock_time(parsed):
            return parsed

    date_local = str(txn.get("date") or "").strip()
    if date_local and _raw_has_clock_time(date_local):
        return parse_ig_transaction_datetime(date_local)

    return parse_ig_transaction_datetime(date_utc or date_local)


def format_closed_trade_datetime(raw: str) -> str:
    """Format closed_at for UI — IG style: DD/MM/YYYY HH:MM."""
    s = str(raw or "").strip()
    if not s:
        return "—"
    has_time = _raw_has_clock_time(s)
    normalised = parse_ig_transaction_datetime(s) or s
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%Y-%m-%d",
    ):
        try:
            part = normalised[:19].replace("T", " ") if "%H" in fmt else normalised[:10]
            dt = datetime.strptime(part, fmt)
            date_txt = dt.strftime("%d/%m/%Y")
            if not has_time:
                return date_txt
            return f"{date_txt} {dt.strftime('%H:%M')}"
        except ValueError:
            continue
    if " " in normalised:
        date_part, time_part = normalised.split(" ", 1)
        try:
            dt = datetime.strptime(date_part[:10], "%Y-%m-%d")
            date_txt = dt.strftime("%d/%m/%Y")
            if not has_time:
                return date_txt
            return f"{date_txt} {time_part[:5]}"
        except ValueError:
            return normalised[:16] if has_time else normalised[:10]
    return normalised[:10]


def parse_signed_ig_size(raw: object) -> tuple[float, str]:
    """
    IG size string includes direction: +1 = BUY, -1 = SELL.
    Returns (abs_size, side).
    """
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return 1.0, "BUY"
    if s.startswith("+"):
        try:
            return abs(float(s[1:])), "BUY"
        except ValueError:
            return 1.0, "BUY"
    if s.startswith("-"):
        try:
            return abs(float(s[1:])), "SELL"
        except ValueError:
            return 1.0, "SELL"
    try:
        v = float(s)
        return abs(v), "BUY" if v >= 0 else "SELL"
    except ValueError:
        return 1.0, "BUY"


def parse_ig_transaction_row(
    txn: dict[str, Any],
    *,
    epic_filter: str = "",
    activity_times: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Normalise one IG transaction into a closed-trade display row."""
    if txn.get("cashTransaction"):
        return None

    txn_type = str(txn.get("transactionType") or "").upper()
    if txn_type and txn_type not in ("DEAL", "POSITION", "TRADE", ""):
        return None

    pnl = parse_ig_money(txn.get("profitAndLoss"))
    if pnl is None:
        pnl = parse_ig_money(txn.get("profitAndLossGBP"))
    if pnl is None:
        return None

    ref = str(txn.get("reference") or txn.get("dealReference") or "").strip()
    deal_id = str(txn.get("dealId") or "").strip()
    if not ref and deal_id:
        ref = deal_id
    if not deal_id and ref:
        deal_id = ref
    if not ref:
        return None

    try:
        open_level = float(txn.get("openLevel") or 0)
    except (TypeError, ValueError):
        open_level = 0.0
    try:
        close_level = float(txn.get("closeLevel") or 0)
    except (TypeError, ValueError):
        close_level = 0.0

    size, side = parse_signed_ig_size(txn.get("size"))
    epic = str(txn.get("epic") or "").strip()
    market = format_market_display_name(
        str(txn.get("instrumentName") or ""),
        epic=epic,
    )
    if epic_filter:
        if epic and epic != epic_filter:
            return None
        if not epic and epic_filter not in market and "Japan" not in market and "225" not in market:
            return None

    currency = str(txn.get("currency") or "GBP").upper()
    activity_ts = lookup_activity_time(
        activity_times,
        deal_id=deal_id,
        deal_reference=ref,
    )
    closed_at = extract_ig_transaction_closed_at(txn, activity_time=activity_ts)
    result = classify_result(pnl)

    deal_reference = str(txn.get("dealReference") or ref or "").strip()
    if deal_reference == deal_id:
        deal_reference = ref if ref != deal_id else deal_reference

    return {
        "closed_at": closed_at,
        "market": market,
        "epic": epic,
        "side": side,
        "entry": open_level,
        "exit": close_level,
        "pnl_points": pnl,
        "ig_pnl_currency": pnl,
        "size": size,
        "result": result,
        "deal_reference": deal_reference or ref,
        "ig_deal_id": deal_id or ref,
        "notes": "IG transaction history",
        "source": "ig",
        "currency": currency,
    }


def filter_rows_last_hours(rows: list[dict[str, Any]], hours: float) -> list[dict[str, Any]]:
    if hours <= 0:
        return rows
    cutoff = datetime.now() - timedelta(hours=hours)
    out: list[dict[str, Any]] = []
    for r in rows:
        raw = str(r.get("closed_at") or "")
        try:
            part = raw[:19]
            try:
                dt = datetime.strptime(part, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                dt = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            out.append(r)
            continue
        if dt >= cutoff:
            out.append(r)
    return out
