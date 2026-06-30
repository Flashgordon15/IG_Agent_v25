export const REGIME_COLORS: Record<string, string> = {
  TRENDING: "rgba(56, 189, 248, 0.08)",
  RANGING: "rgba(148, 163, 184, 0.08)",
  VOLATILE: "rgba(251, 191, 36, 0.10)",
  QUIET: "rgba(52, 211, 153, 0.08)",
  UNKNOWN: "rgba(15, 23, 42, 0.0)",
};

export const REGIME_ACCENT: Record<string, string> = {
  TRENDING: "#38bdf8",
  RANGING: "#94a3b8",
  VOLATILE: "#fbbf24",
  QUIET: "#34d399",
  UNKNOWN: "#64748b",
};

export function regimeColor(regime: string): string {
  const key = regime.toUpperCase().replace(/\s+/g, "_");
  for (const [k, v] of Object.entries(REGIME_COLORS)) {
    if (key.includes(k)) return v;
  }
  return REGIME_COLORS.UNKNOWN;
}

export function regimeAccent(regime: string): string {
  const key = regime.toUpperCase().replace(/\s+/g, "_");
  for (const [k, v] of Object.entries(REGIME_ACCENT)) {
    if (key.includes(k)) return v;
  }
  return REGIME_ACCENT.UNKNOWN;
}
