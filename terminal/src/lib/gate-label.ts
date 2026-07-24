/**
 * Display mapper for Absolute Strategy Execution Truth gate verdicts.
 * Maps sparse/unknown agent payloads to an honest standby label —
 * never invents SETUP / ARMED / PASSED.
 */

export const GATE_STANDBY_WAITING_BREAKOUT = "GATE_STANDBY_WAITING_BREAKOUT";

const UNKNOWN_ALIASES = new Set([
  "",
  "GATE_UNKNOWN",
  "UNKNOWN",
  "N/A",
  "NA",
  "NONE",
  "NULL",
]);

/**
 * Normalize a raw gate verdict for operator-facing chrome.
 * Preserves real veto/wait/passed strings; remaps blank/unknown only.
 */
export function mapGateVerdictLabel(raw: string | null | undefined): string {
  const u = String(raw ?? "")
    .trim()
    .toUpperCase()
    .replace(/\s+/g, "_");
  if (UNKNOWN_ALIASES.has(u)) {
    return GATE_STANDBY_WAITING_BREAKOUT;
  }
  return u;
}
