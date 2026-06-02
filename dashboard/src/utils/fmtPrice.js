/** Instrument-aware price display (epic from IG or dashboard state). */
export function fmtPrice(v, epic) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const key = String(epic || "").toUpperCase();
  if (key.includes("EURUSD") || key.includes("EUR")) {
    return Number(v).toFixed(5);
  }
  if (key.includes("GOLD") || key.includes("CFPGOLD") || key.includes("GC")) {
    return Number(v).toFixed(2);
  }
  return Number(v).toFixed(1);
}
