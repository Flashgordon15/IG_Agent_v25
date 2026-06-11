"""OHLC JSONL cache paths per instrument epic."""

from __future__ import annotations

from pathlib import Path

from system.paths import data_dir

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


def ohlc_cache_path(epic: str, market: str = "") -> Path:
    """Resolve append-only 5m cache file for an IG epic."""
    key = str(epic or "").strip()
    filename = _EPIC_CACHE_FILES.get(key)
    if not filename:
        slug = (
            str(market or key)
            .lower()
            .replace("/", "")
            .replace(" ", "_")
            .replace(".", "_")
        )
        filename = f"{slug or 'market'}_5m.jsonl"
    return data_dir() / "ohlc_cache" / filename
