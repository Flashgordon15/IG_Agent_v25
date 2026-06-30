export type JsonObject = Record<string, unknown>;

export type StatusLevel = "ok" | "warn" | "error";

export type LogLevel = "info" | "warn" | "error";

export type LogSubsystem =
  | "all"
  | "feeds"
  | "routing"
  | "sizing"
  | "governance"
  | "execution";

export type RouteState = "active" | "warming" | "degraded" | "idle";

export type EnvelopeState = "active" | "breached" | "disabled";

export type OrderValveState = "open" | "suppressed" | "warming";

export type PanelFocus = "logs" | "routing" | "strategy" | null;

export type WsConnectionState =
  | "connected"
  | "reconnecting"
  | "degraded"
  | "disconnected";

export type BootStepStatus =
  | "pending"
  | "running"
  | "ok"
  | "degraded"
  | "failed"
  | "healing";

export interface BootStageRow {
  id: string;
  label: string;
  status: BootStepStatus;
  lastError?: string;
  retryCount?: number;
  elapsedMs?: number | null;
}

export interface BootSubsystemRow {
  id: string;
  label: string;
  status: BootStepStatus;
  lastError?: string;
  retryCount?: number;
  healAction?: string;
}

export interface BootStatusView {
  tradeReady: boolean;
  currentStage: string;
  bootElapsedSec: number;
  estimatedReadySec: number | null;
  stages: BootStageRow[];
  subsystems: BootSubsystemRow[];
  blockers: string[];
  startupDiagnostics?: StartupDiagnosticRow[];
}

export interface StartupDiagnosticRow {
  key: string;
  label: string;
  ok: boolean;
}

export interface StatusRow {
  key: string;
  label: string;
  status: StatusLevel;
  detail: string;
  hint?: string;
  progress?: number;
  updatedAt?: string;
}

export interface RouteRow {
  epic: string;
  venue: string;
  path: string;
  state: RouteState;
  confidence: number;
  latencyMs: number | null;
  fillQuality: string;
  slippageBps: number | null;
  slippageTrend: "up" | "down" | "flat";
  reason: string;
  flags: string[];
}

export interface RiskItem {
  label: string;
  value: string;
  status: StatusLevel;
  explanation: string;
}

export interface GovernanceFlag {
  label: string;
  active: boolean;
  detail: string;
}

export interface LogLine {
  id: string;
  ts: string;
  message: string;
  level: LogLevel;
  subsystem: LogSubsystem;
}

export interface SignalMarker {
  time: number;
  position: "aboveBar" | "belowBar";
  color: string;
  shape: "arrowUp" | "arrowDown" | "circle";
  text: string;
}

export interface PricePoint {
  time: number;
  value: number;
}

export interface CandlePoint {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface RiskBand {
  upper: number;
  lower: number;
  label: string;
}

export interface CockpitSnapshot {
  guiStatus: JsonObject | null;
  pnl: JsonObject | null;
  routing: JsonObject | null;
  risk: JsonObject | null;
  logs: JsonObject | null;
  tick: JsonObject | null;
  wsConnected: boolean;
  loading: boolean;
  ready: boolean;
  errors: string[];
  lastUpdated: string | null;
  tickLatencyMs: number | null;
}

export interface TopBarView {
  regime: string;
  regimeColor: string;
  strategy: string;
  pnlGbp: number | null;
  sessionStatus: string;
  wsConnected: boolean;
  wsState: WsConnectionState;
  feedStatus: string;
  feedHealth: "ok" | "warn" | "error";
  latencyMs: number | null;
  latencyLevel: "ok" | "warn" | "error";
  tradingPaused: boolean;
  orderValve: OrderValveState;
}

export interface ChartViewModel {
  epic: string;
  regime: string;
  regimeColor: string;
  candleHistory: CandlePoint[];
  pnlHistory: PricePoint[];
  markers: SignalMarker[];
  riskBands: RiskBand[];
  targetPnl: number | null;
  currentPnl: number | null;
}
