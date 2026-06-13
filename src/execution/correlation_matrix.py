"""
Portfolio correlation density scaler — independent of correlation_guard hard caps.

Counts open positions in the same statistical cluster as a candidate epic and
returns a size risk multiplier (1.0 → 0.5 → 0.25). Does not block entries;
correlation_guard remains the hard gate for per-epic / global caps.
"""

from __future__ import annotations

from typing import Any

# --- Correlation groups (epic → bucket) ---
GROUP_US_EQUITY = "us_equity_index"
GROUP_ASIA_EQUITY = "asia_equity_index"
GROUP_EU_EQUITY = "eu_equity_index"
GROUP_FX_USD = "fx_usd"
GROUP_FX_CROSS = "fx_cross"
GROUP_COMMODITY_METALS = "commodity_metals"
GROUP_COMMODITY_ENERGY = "commodity_energy"
GROUP_OTHER = "other"

_EPIC_GROUP: dict[str, str] = {
    "IX.D.DOW.IFM.IP": GROUP_US_EQUITY,
    "IX.D.NASDAQ.IFM.IP": GROUP_US_EQUITY,
    "IX.D.SP500.IFM.IP": GROUP_US_EQUITY,
    "IX.D.NIKKEI.IFM.IP": GROUP_ASIA_EQUITY,
    "IX.D.DAX.IFM.IP": GROUP_EU_EQUITY,
    "CS.D.EURUSD.CFD.IP": GROUP_FX_USD,
    "CS.D.GBPUSD.CFD.IP": GROUP_FX_USD,
    "CS.D.CFPGOLD.CFP.IP": GROUP_COMMODITY_METALS,
    "CS.D.CRUDE.CFD.IP": GROUP_COMMODITY_ENERGY,
}

# Clusters merge groups that move together for density counting.
_CLUSTER_BY_GROUP: dict[str, str] = {
    GROUP_US_EQUITY: "global_equity",
    GROUP_ASIA_EQUITY: "global_equity",
    GROUP_EU_EQUITY: "global_equity",
    GROUP_FX_USD: "fx_usd_block",
    GROUP_FX_CROSS: "fx_cross_block",
    GROUP_COMMODITY_METALS: "commodity_metals",
    GROUP_COMMODITY_ENERGY: "commodity_energy",
    GROUP_OTHER: "other",
}

# Open positions in cluster → size multiplier for a new entry in that cluster.
_DENSITY_MULTIPLIERS: tuple[tuple[int, float], ...] = (
    (0, 1.0),
    (1, 0.75),
    (2, 0.5),
    (3, 0.25),
)


def epic_correlation_group(epic: str) -> str:
    key = str(epic or "").strip()
    return _EPIC_GROUP.get(key, GROUP_OTHER)


def epic_correlation_cluster(epic: str) -> str:
    group = epic_correlation_group(epic)
    return _CLUSTER_BY_GROUP.get(group, group)


def _position_epic(row: dict[str, Any]) -> str:
    return str(row.get("epic") or row.get("instrument_epic") or "").strip()


def correlation_density(
    epic: str,
    open_positions: list[dict[str, Any]] | None,
) -> int:
    """How many open positions share the candidate epic's correlation cluster."""
    cluster = epic_correlation_cluster(epic)
    count = 0
    for row in open_positions or []:
        if not isinstance(row, dict):
            continue
        pos_epic = _position_epic(row)
        if not pos_epic:
            continue
        if epic_correlation_cluster(pos_epic) == cluster:
            count += 1
    return count


def multiplier_for_density(density: int) -> float:
    d = max(0, int(density))
    mult = _DENSITY_MULTIPLIERS[-1][1]
    for threshold, m in _DENSITY_MULTIPLIERS:
        if d <= threshold:
            return float(m)
    return float(mult)


def correlation_density_risk_multiplier(
    epic: str,
    open_positions: list[dict[str, Any]] | None,
) -> tuple[float, int, str]:
    """
    Return (size_multiplier, density_count, detail) for position sizing.

    Multiplier applies to baseline size before IG min / risk-cap clipping.
    """
    density = correlation_density(epic, open_positions)
    mult = multiplier_for_density(density)
    cluster = epic_correlation_cluster(epic)
    group = epic_correlation_group(epic)
    if mult >= 1.0:
        detail = f"corr density {density} ({cluster}) ×1.0"
    else:
        detail = (
            f"corr density {density} in {cluster}/{group} "
            f"→ size ×{mult:.2f}"
        )
    return mult, density, detail


def reset_correlation_matrix_for_tests() -> None:
    """No persistent state — hook for test symmetry."""
    return None
