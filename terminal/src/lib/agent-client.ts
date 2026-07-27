/** Agent REST + WebSocket base — browser uses same-origin rewrites in dev. */

import { cfdHttpBase, deskWsBase as cfdDeskWsBase } from "@/lib/desk-api-bases";

export function agentHttpBase(): string {
  return cfdHttpBase();
}

export function agentWsBase(): string {
  return cfdDeskWsBase("cfd");
}

export async function fetchAgentJson<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 4000,
): Promise<T> {
  const url = `${agentHttpBase()}${path.startsWith("/") ? path : `/${path}`}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
    });
    if (!res.ok) {
      throw new Error(`Agent ${path} HTTP ${res.status}`);
    }
    return res.json() as Promise<T>;
  } catch (e) {
    if (e instanceof Error && e.name === "AbortError") {
      throw new Error(`Agent ${path} timed out after ${timeoutMs}ms`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export type GateEntry = {
  name: string;
  passed: boolean;
  detail?: string;
  why_failed?: string;
  value?: unknown;
};

export type GateDiagnosticRow = {
  epic: string;
  updated_at?: string;
  all_passed?: boolean;
  wait_reason?: string;
  gating_reason?: string;
  gates?: GateEntry[];
  tuning?: Record<string, unknown>;
};

export type MarketQuote = {
  epic?: string;
  bid: number;
  offer: number;
  mid: number;
  last_price: number;
  source?: string;
};

export type FrontierEpicRow = {
  epic?: string;
  direction?: string;
  vector?: { rsi?: number; atr?: number; momentum?: number };
  zone_label?: string;
  wait_reason?: string;
  all_passed?: boolean;
};

export type PerformanceRow = {
  epic?: string;
  direction?: string;
  action?: string;
  size?: number;
  entry?: number;
  exit?: number;
  executed_at?: string;
  closed_at?: string;
  source?: string;
};

export type FulfillmentPayload = {
  mode?: string;
  updated_at?: string;
  server_mono_ms?: number;
  gate_diagnostics?: { by_epic?: Record<string, GateDiagnosticRow>; last?: GateDiagnosticRow };
  alpha_frontier_tracker?: {
    by_epic?: Record<string, FrontierEpicRow>;
    last?: FrontierEpicRow;
  };
  performance_rows?: PerformanceRow[];
  ticks_cached?: number;
  all_ready?: boolean;
  trading_paused?: boolean;
  quote_freshness?: {
    budget_sec?: number;
    age_sec?: number;
    fresh?: boolean;
    fail_closed?: boolean;
  };
  market_quotes?: Record<string, MarketQuote>;
  market_quotes_list?: MarketQuote[];
  memory_alignment?: string;
  data_velocity?: Record<string, unknown>;
  volatility_z_score?: number;
  ui_stress_render?: {
    active?: boolean;
    hz?: number;
    epic?: string;
    poll_ms?: number;
  };
  /** Optional adaptive-logistics tune mirror (command deck). */
  tuning_variables?: Record<string, unknown>;
};

export type TelemetryPayload = Record<string, unknown>;

export type TriageLedgerPayload = {
  rows?: Array<Record<string, unknown>>;
};

export type TradesPayload = {
  trades?: Array<Record<string, unknown>>;
  active?: Array<Record<string, unknown>>;
  closed?: Array<Record<string, unknown>>;
};

export type AgentHealth = {
  ok?: boolean;
  trading_healthy?: boolean;
  status?: string;
  ready?: boolean;
  agent_alive?: boolean;
  port_bound?: boolean;
  agent_pid?: number;
  /** A2 / operator entry freeze — prefer over fulfillment when set. */
  trading_paused?: boolean;
  trade_ready?: boolean;
};

/** Desk port online when HTTP health responds and engine is trading-ready (orchestrator-aligned). */
export function isDeskHealthOperational(
  health: AgentHealth | null | undefined,
): boolean {
  if (!health || typeof health !== "object") return false;
  if (health.ok === true) return true;
  if (health.trading_healthy === true) return true;
  const status = String(health.status ?? "").toUpperCase();
  if (status === "OPERATIONAL") return true;
  if (health.ready === true && health.agent_alive !== false) return true;
  if (health.port_bound === true && health.agent_alive === true) return true;
  return false;
}

export type ProtectionSummary = {
  mode?: string;
  layers_armed?: boolean;
  gbp_armed?: boolean;
  virtual_armed?: boolean;
  dynamic_armed?: boolean;
  loss_cap_gbp?: number | null;
  soft_loss_gbp?: number | null;
  target_gbp?: number | null;
  trail_floor_gbp?: number | null;
  peak_profit_gbp?: number | null;
  broker_stop_level?: number | null;
  broker_limit_level?: number | null;
  operator_note?: string;
};

export type LivePositionRow = {
  deal_id: string;
  epic: string;
  direction: string;
  size: number;
  entry: number;
  pnl_gbp: number | null;
  gbp_armed: boolean;
  virtual_armed: boolean;
  dynamic_armed: boolean;
  peak_profit_gbp?: number | null;
  trail_floor_gbp?: number | null;
  loss_cap_gbp?: number | null;
  soft_loss_gbp?: number | null;
  target_gbp?: number | null;
  source?: string;
  flatten_failed?: boolean;
  flatten_error?: string | null;
  critical_alarm?: boolean;
  protection_summary?: ProtectionSummary;
};

export type TradeSupportBlock = {
  running?: boolean;
  broker_open?: number;
  valued?: number;
  total_unrealized_gbp?: number | null;
  status_age_sec?: number | null;
  actions_failed?: number;
  last_flatten_error?: string | null;
};

export type BrokerOpenSot = {
  count?: number;
  source?: string;
  trade_support_open?: number;
  rows_open?: number;
  total_pnl_gbp?: number;
  note?: string;
};

export type LivePositionsPayload = {
  ok: boolean;
  error?: string;
  count: number;
  total_pnl_gbp?: number;
  unmonitored?: number;
  layers_incomplete?: number;
  verdict?: string;
  stale?: boolean;
  critical?: boolean;
  critical_alarms?: string[];
  protection_note?: string;
  trade_support?: TradeSupportBlock | null;
  broker_open_sot?: BrokerOpenSot | null;
  last_good_age_sec?: number;
  sync_age_sec?: number | null;
  sync_status?: string;
  positions: LivePositionRow[];
};

export type TradingDeskLiveness = {
  ok?: boolean;
  connections?: Record<string, boolean>;
  issues?: string[];
  degraded_streak?: number;
  recovery_count?: number;
  has_open_risk?: boolean;
  open_count?: number;
  unmonitored?: number;
};

/** v31 adaptive logistics tune payload (e8f628c command deck). */
export type V31TunePayload = {
  ok?: boolean;
  ml_veto_override?: number | null;
  alpha_seed_override?: number | null;
  auto_decay_enabled?: boolean;
  clear_overrides?: boolean;
};

export async function postAgentTune(
  body: V31TunePayload,
): Promise<Record<string, unknown>> {
  return fetchAgentJson<Record<string, unknown>>("/api/v31/tune", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
