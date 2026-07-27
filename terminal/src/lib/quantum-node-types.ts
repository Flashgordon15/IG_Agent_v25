/** Hybrid Multi-Asset Quantum Node — live desk telemetry contracts. */

export type AssetNodeId = "dow" | "brent" | "vix" | "gold";

export type AssetMarketNode = {
  id: AssetNodeId;
  slot: number;
  label: string;
  epic: string;
  mid: number;
  bid: number;
  offer: number;
  /** Primary metric line (entry / oil / vix / gold mid) */
  primaryLabel: string;
  primaryValue: string;
  /** Secondary (PnL / alert / risk / proxy) */
  secondaryLabel: string;
  secondaryValue: string;
  secondaryTone: "ok" | "warn" | "bad" | "mute";
  /** Tertiary chip (VETO / MACRO / etc.) */
  chip: string;
  chipTone: "ok" | "warn" | "bad" | "mute";
  /** Tick ring for SVG sparkline (newest at end) */
  ticks: number[];
  tickLatencyMs: number;
  source: string;
};

export type AlphaPoint = {
  t: number;
  mid: number;
  obi: number; // clamped ±0.15
};

export type PositionAuthorityRow = {
  dealId: string;
  epic: string;
  label: string;
  direction: string;
  size: number;
  entry: number;
  /** Live mid from fulfillment / WS quote ring */
  currentMid: number | null;
  pnlGbp: number | null;
  peakProfitGbp: number | null;
  atr: number;
  atrMultiple: number;
  takeProfit: number | null;
  softLossGbp: number | null;
  trailFloorGbp: number | null;
  targetGbp: number | null;
  b1FloorGbp: number;
  b1Status: "OK" | "BREACHED" | "UNKNOWN";
  layers: string;
  bias: string;
};

/** AI multi-market sniper scanner row (DOW / FTSE / Gold / EURUSD). */
export type SniperStatusKind = "long" | "short" | "proxy";

/** Ranked rotator lane for scanner chrome (promoted ≠ legacy DOW-only stack). */
export type SniperRankLane =
  | "promoted"
  | "eligible"
  | "waiting"
  | "excluded"
  | "stack"
  | null;

export type SniperMarketRow = {
  id: "dow" | "ftse" | "gold" | "eurusd" | "dax" | "brent";
  label: string;
  epic: string;
  mid: number;
  ticks: number[];
  statusKind: SniperStatusKind;
  statusText: string;
  profile: string;
  /** 0–1 ML / AI conviction when available */
  conviction: number | null;
  sniperThreshold: number;
  velocity: number;
  zScore: number;
  tpm: number;
  inActiveStack: boolean;
  /** Ranked top-N allowlist member when ranked rotator is active */
  inPromoted: boolean;
  rankLane: SniperRankLane;
  rank: number | null;
  allowEntries: boolean;
  regimeLabel: string;
  /** RuntimeContext metric overrides (memory plane) */
  maxSpreadPts: number;
  pointMultiplier: number;
  isForex: boolean;
  /** Live multiplex open-book overlays */
  openCount: number;
  openDirection: string | null;
  pnlGbp: number | null;
  trailFloorGbp: number | null;
  softLossGbp: number | null;
  trailPriceLevel: number | null;
};

export type ScannerRankedChrome = {
  active: boolean;
  dominant: string | null;
  promotedLabels: string[];
  waitingLabels: string[];
  excludedNote: string | null;
};

export type QuantumSafetyMatrix = {
  driverIntegrity: "OK" | "DEGRADED" | "FAULT";
  cudaCompiler: "READY" | "COMPILING" | "ERROR";
  databaseSync: "SYNCED" | "LAGGING" | "OFFLINE";
  thermalTrip: boolean;
  feedLabel: string;
  memoryAlignment: string;
  macroBias: string;
  lastUpdatedMs: number;
};

export type QuantumNodeView = {
  nodes: AssetMarketNode[];
  scanner: SniperMarketRow[];
  /** Ranked rotator summary for scanner chips (SB-prefer when CFD paused) */
  rankedChrome: ScannerRankedChrome;
  alpha: {
    epic: string;
    label: string;
    series: AlphaPoint[];
    lastMid: number;
    lastObi: number;
  };
  positions: PositionAuthorityRow[];
  safety: QuantumSafetyMatrix;
  tick: number;
  totalUplGbp: number | null;
  wsState: string;
  /** Single desk-level idle reason (us_close / insufficient_bars / gate) */
  deskIdleReason?: string | null;
};
