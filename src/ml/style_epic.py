"""Style (sniper|long) + epic stamping helpers for the ML learning plane.

Pure-stdlib so both the low-level ``data.ml_training_store`` writer and the
offline replay/backfill tooling can import it without cycles.

* ``sniper`` — CFD micro / QUANT_SNIPER scalp path.
* ``long``   — SB macro / long_trade_runner (LTR) path.
* ``unknown`` — not derivable; never fabricated.

The hold-based split mirrors ``diagnostics.performance_journal.infer_trade_style``
(the 180s long_trade_runner arm window) so this helper stays consistent with the
journal instead of inventing a second convention.
"""

from __future__ import annotations

from typing import Any

# Long_trade_runner arms after ~3m in profit; holds at/over this are macro/long.
LONG_HOLD_SEC = 180.0

SNIPER = "sniper"
LONG = "long"
UNKNOWN = "unknown"

# IG epic -> canonical display name (mirror of ohlc_yahoo_seeder.EPIC_YAHOO_MAP)
# plus the reverse lookup used to recover an epic from a legacy instrument label.
_EPIC_BY_DISPLAY: dict[str, str] = {
    "wall street": "IX.D.DOW.IFM.IP",
    "dow": "IX.D.DOW.IFM.IP",
    "dow jones": "IX.D.DOW.IFM.IP",
    "us wall street 30": "IX.D.DOW.IFM.IP",
    "japan 225": "IX.D.NIKKEI.IFM.IP",
    "nikkei": "IX.D.NIKKEI.IFM.IP",
    "nikkei 225": "IX.D.NIKKEI.IFM.IP",
    "spot gold": "CS.D.CFPGOLD.CFP.IP",
    "gold": "CS.D.CFPGOLD.CFP.IP",
    "eur/usd": "CS.D.EURUSD.CFD.IP",
    "eurusd": "CS.D.EURUSD.CFD.IP",
    "gbp/usd": "CS.D.GBPUSD.CFD.IP",
    "gbpusd": "CS.D.GBPUSD.CFD.IP",
    "germany 40": "IX.D.DAX.IFM.IP",
    "dax": "IX.D.DAX.IFM.IP",
    "germany 40 cash": "IX.D.DAX.IFM.IP",
    "us oil wti": "CS.D.CRUDE.CFD.IP",
    "us crude": "CS.D.CRUDE.CFD.IP",
    "ftse 100": "IX.D.FTSE.IFM.IP",
    "us tech 100": "IX.D.NASDAQ.IFM.IP",
    "us tech 100 cash": "IX.D.NASDAQ.IFM.IP",
}


def epic_for_instrument(instrument: Any, *, fallback_epic: Any = "") -> str:
    """Best-effort epic for a display name/instrument label.

    Prefers an explicit ``fallback_epic`` when it already looks like an IG epic
    (contains a dot), otherwise maps the display label. Returns ``""`` when not
    resolvable — callers should keep that as an honest blank rather than guess.
    """
    fb = str(fallback_epic or "").strip()
    if fb and "." in fb:
        return fb
    name = str(instrument or "").strip()
    if not name:
        return fb
    if "." in name and name.upper() == name.replace(" ", ""):
        # Looks like an epic already (e.g. IX.D.DOW.IFM.IP).
        return name
    return _EPIC_BY_DISPLAY.get(name.lower(), fb)


def resolve_ml_style(
    *,
    style_hint: Any = None,
    engine_origin: Any = "",
    exit_reason: Any = "",
    hold_sec: Any = None,
) -> str:
    """Resolve ``sniper|long|unknown`` for a learning row.

    Priority: explicit hint -> engine origin -> exit reason -> hold-based split.
    Never returns a fabricated style; falls back to ``unknown``.
    """
    hint = str(style_hint or "").strip().lower()
    if hint in (SNIPER, "scalp", "micro"):
        return SNIPER
    if hint in (LONG, "macro"):
        return LONG

    origin = str(engine_origin or "").upper()
    if any(tok in origin for tok in ("SNIPER", "MICRO", "SCALP", "QUANT")):
        return SNIPER
    if any(tok in origin for tok in ("SENTINEL", "MACRO", "LONG", "LTR", "RUNNER")):
        return LONG

    reason = str(exit_reason or "").lower()
    if "long_runner" in reason or "long_trade" in reason or "runner_extended" in reason:
        return LONG

    try:
        if hold_sec is not None:
            hs = float(hold_sec)
            if hs >= LONG_HOLD_SEC:
                return LONG
            if hs >= 0:
                return SNIPER
    except (TypeError, ValueError):
        pass

    if hint == "supervised_exit":
        return UNKNOWN
    return UNKNOWN
