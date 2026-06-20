/** Apex avionics — canonical asset keys and operational envelope constants. */

export const APEX_FRAME_COLOR = "#090d1f";

export const ASSET_KEYS = ["GOLD", "WALL_STREET", "JAPAN_225", "EUR_USD"];

export const EPIC_TO_ASSET = {
  "CS.D.CFPGOLD.CFP.IP": "GOLD",
  "IX.D.DOW.IFM.IP": "WALL_STREET",
  "IX.D.NIKKEI.IFM.IP": "JAPAN_225",
  "CS.D.EURUSD.CFD.IP": "EUR_USD",
};

export const ASSET_LABELS = {
  GOLD: "Gold",
  WALL_STREET: "Wall Street",
  JAPAN_225: "Japan 225",
  EUR_USD: "EUR/USD",
};

export const ASSET_COLORS = {
  GOLD: 0xfbbf24,
  WALL_STREET: 0x3b82f6,
  JAPAN_225: 0xa855f7,
  EUR_USD: 0x22d3ee,
};

/** £10k real-money baseline · £750 concurrent envelope · 0.450 ML veto floor */
export const BASELINE_EQUITY_GBP = 10_000;
export const PORTFOLIO_ENVELOPE_GBP = 750;
export const ML_VETO_FLOOR = 0.45;

/** Strategy execution floors surfaced on WebGL risk walls */
export const EXEC_CONFIDENCE_FLOOR = 45;
export const EXEC_FITNESS_FLOOR_PCT = 55;

/** Primary WebGL torus tracks — left Gold, right Wall St */
export const PRIMARY_RING_KEYS = ["GOLD", "WALL_STREET"];

export const OPERATIONAL_PILLARS = [
  { id: "A", label: "Sizing & RR", key: "pillar_a" },
  { id: "B", label: "Session Rotation", key: "pillar_b" },
  { id: "C", label: "Synthetic Replay", key: "pillar_c" },
  { id: "D", label: "Liquidity Shield", key: "pillar_d" },
  { id: "E", label: "Hardware Telemetry", key: "pillar_e" },
];

export const IPC_STALE_MS = 5000;
export const TICK_LERP_MS = 320;
