"""OHLC JSONL cache paths per instrument epic."""

from __future__ import annotations

from pathlib import Path

from system.paths import data_dir, legacy_src_data_dir

_EPIC_CACHE_FILES: dict[str, str] = {
    "IX.D.NIKKEI.IFM.IP": "nikkei_5m.jsonl",
    "CS.D.EURUSD.CFD.IP": "eurusd_5m.jsonl",
    "CS.D.CFPGOLD.CFP.IP": "gold_5m.jsonl",
    "CS.D.GBPUSD.CFD.IP": "gbpusd_5m.jsonl",
    "IX.D.DOW.IFM.IP": "wall_street_5m.jsonl",
    "IX.D.NASDAQ.IFM.IP": "nasdaq_100_5m.jsonl",
    "CS.D.CRUDE.CFD.IP": "us_oil_wti_5m.jsonl",
    "IX.D.DAX.IFM.IP": "germany_40_5m.jsonl",
}


def _filename_for_epic(epic: str, market: str = "") -> str:
    key = str(epic or "").strip()
    filename = _EPIC_CACHE_FILES.get(key)
    if filename:
        return filename
    slug = (
        str(market or key)
        .lower()
        .replace("/", "")
        .replace(" ", "_")
        .replace(".", "_")
    )
    return f"{slug or 'market'}_5m.jsonl"


def ohlc_cache_path(epic: str, market: str = "") -> Path:
    """Resolve append-only 5m cache file for an IG epic.

    Prefer the unified data root. When that file is missing (common after
    v31 data-root unification left an empty ``ohlc_cache/``), fall back to
    the legacy ``src/data/ohlc_cache`` tree so regime bars can warm.
    """
    filename = _filename_for_epic(epic, market)
    primary = data_dir() / "ohlc_cache" / filename
    if primary.is_file() and primary.stat().st_size > 0:
        return primary
    legacy = legacy_src_data_dir() / "ohlc_cache" / filename
    if legacy.is_file() and legacy.stat().st_size > 0:
        return legacy
    return primary
