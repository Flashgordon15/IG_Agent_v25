/**
 * Pure helpers for GPU execution channel series hygiene.
 * Keeps NaN/Inf out of canvas paths and defines the empty-state watermark.
 */

export const CHART_EMPTY_WATERMARK = "NO SERIES · FLAT / WARMING";

/** Keep only finite, positive price samples (reject NaN / Inf / 0 / negatives). */
export function sanitizePriceSeries(
  values: Array<number | null | undefined>,
): number[] {
  const out: number[] = [];
  for (const v of values) {
    if (typeof v !== "number") continue;
    if (!Number.isFinite(v) || !(v > 0)) continue;
    out.push(v);
  }
  return out;
}

/** True when a mid path can be stroked (needs ≥2 clean samples). */
export function chartHasDrawableSeries(series: number[]): boolean {
  return sanitizePriceSeries(series).length >= 2;
}

/** Accept a single mid push into the ring buffer. */
export function isValidMidSample(mid: number): boolean {
  return typeof mid === "number" && Number.isFinite(mid) && mid > 0;
}
