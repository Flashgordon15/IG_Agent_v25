/** Instrument-aware price display (epic from IG or dashboard state). */
export function fmtPrice(v, epic) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const key = String(epic || "").toUpperCase();
  // Forex pairs — always 5 decimal places
  if (
    key.includes("EURUSD") || key.includes("GBPUSD") ||
    key.includes("GBPEUR") || key.includes("USDCAD") ||
    key.includes("CS.D.EUR") || key.includes("CS.D.GBP") ||
    key.includes("=X") || key.includes("EURUSD") ||
    key.includes("EUR") || key.includes("GBP")
  ) {
    return Number(v).toFixed(5);
  }
  // Gold and other CFP instruments — 2 decimal places
  if (key.includes("GOLD") || key.includes("CFPGOLD") || key.includes("GC=F")) {
    return Number(v).toFixed(2);
  }
  // Indices and oil — 1 decimal place
  return Number(v).toFixed(1);
}
