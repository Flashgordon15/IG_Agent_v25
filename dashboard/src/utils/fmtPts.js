/** Instrument-aware open P&L points (FX uses pip-style IG points). */
export function isFxEpic(epic) {
  const key = String(epic || "").toUpperCase();
  if (!key.startsWith("CS.D.") || !key.includes("CFD")) return false;
  if (
    key.includes("CFPGOLD") ||
    key.includes("CFPSILVER") ||
    key.includes("CFPPLAT") ||
    key.includes("CRUDE") ||
    key.includes("OIL")
  ) {
    return false;
  }
  return (
    key.includes("EURUSD") ||
    key.includes("GBPUSD") ||
    key.includes("AUDUSD") ||
    key.includes("EURGBP") ||
    key.includes("USDCAD") ||
    key.includes("NZDUSD") ||
    key.includes("USDCHF") ||
    key.includes("USDJPY") ||
    key.includes("EURJPY") ||
    key.includes("GBPJPY")
  );
}

export function fmtPts(v, epic) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  const decimals = isFxEpic(epic) ? 2 : 1;
  return `${sign}${n.toFixed(decimals)}`;
}
