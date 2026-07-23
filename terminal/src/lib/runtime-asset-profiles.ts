/**
 * Mirrors `src/system/memory_context.RuntimeContext` asset metric profiles.
 * Used by Command Deck scanner / GPU canvas — no disk I/O.
 */

export type RuntimeAssetProfile = {
  key: string;
  epic: string;
  label: string;
  maxSpreadPts: number;
  pointMultiplier: number;
  isForex: boolean;
  trailNoisePts: number;
};

export const EPIC_DOW = "IX.D.DOW.IFM.IP";
export const EPIC_FTSE = "IX.D.FTSE.IFM.IP";
export const EPIC_GOLD = "CS.D.CFPGOLD.CFP.IP";
export const EPIC_EURUSD = "CS.D.EURUSD.CFD.IP";

export const RUNTIME_ASSET_PROFILES: RuntimeAssetProfile[] = [
  {
    key: "IX.D.DOW.IFM.IP",
    epic: EPIC_DOW,
    label: "WALL ST / DOW",
    maxSpreadPts: 3.0,
    pointMultiplier: 1.0,
    isForex: false,
    trailNoisePts: 1.0,
  },
  {
    key: "UK100",
    epic: EPIC_FTSE,
    label: "FTSE 100",
    maxSpreadPts: 4.5,
    pointMultiplier: 2.0,
    isForex: false,
    trailNoisePts: 1.5,
  },
  {
    key: "GC",
    epic: EPIC_GOLD,
    label: "GOLD",
    maxSpreadPts: 40.0,
    pointMultiplier: 10.0,
    isForex: false,
    trailNoisePts: 4.0,
  },
  {
    key: "EURUSD",
    epic: EPIC_EURUSD,
    label: "EUR/USD",
    maxSpreadPts: 2.0,
    pointMultiplier: 10000.0,
    isForex: true,
    trailNoisePts: 0.5,
  },
];

const BY_EPIC = new Map<string, RuntimeAssetProfile>();
for (const p of RUNTIME_ASSET_PROFILES) {
  BY_EPIC.set(p.epic.toUpperCase(), p);
  BY_EPIC.set(p.key.toUpperCase(), p);
}

export function resolveRuntimeAssetProfile(
  epic: string | null | undefined,
): RuntimeAssetProfile {
  const key = String(epic || "").trim().toUpperCase();
  if (key && BY_EPIC.has(key)) return BY_EPIC.get(key)!;
  if (key.includes("DOW")) return BY_EPIC.get(EPIC_DOW)!;
  if (key.includes("FTSE") || key === "UK100") return BY_EPIC.get(EPIC_FTSE)!;
  if (key.includes("CFPGOLD") || key.includes("GOLD") || key === "GC") {
    return BY_EPIC.get(EPIC_GOLD)!;
  }
  if (key.includes("EURUSD") || key.includes("EUR/USD")) {
    return BY_EPIC.get(EPIC_EURUSD)!;
  }
  return BY_EPIC.get(EPIC_DOW)!;
}

/** Trail step → price delta using contract point multiplier. */
export function trailPriceDelta(epic: string, trailPts: number): number {
  const prof = resolveRuntimeAssetProfile(epic);
  const pts = Number(trailPts);
  if (!Number.isFinite(pts)) return 0;
  if (prof.isForex) return pts / Math.max(prof.pointMultiplier, 1);
  return pts * Math.max(prof.pointMultiplier, 1);
}

/** Reject poisoned mids (Yahoo ~100 on EURUSD, micro-channel on indices). */
export function plausibleMidForEpic(
  epic: string | null | undefined,
  mid: number,
): boolean {
  if (!(mid > 0) || !Number.isFinite(mid)) return false;
  const e = String(epic || "").toUpperCase();
  if (e.includes("EURUSD") || e.includes("GBPUSD") || e.includes("FX")) {
    return mid > 0.5 && mid < 2.5;
  }
  if (e.includes("GOLD") || e.includes("XAU") || e.includes("CFPGOLD")) {
    return mid >= 500 && mid <= 20000;
  }
  if (e.includes("CRUDE") || e.includes("BRENT") || e.includes("OIL")) {
    return mid >= 20 && mid < 500;
  }
  if (
    e.includes("DOW") ||
    e.includes("DAX") ||
    e.includes("NIKKEI") ||
    e.includes("FTSE") ||
    e.includes("IFM") ||
    e.includes("IX.")
  ) {
    return mid >= 1000;
  }
  // Reject notorious ~100 micro-channel / DXY band for unknown epics
  if (mid >= 50 && mid <= 200) return false;
  return true;
}
