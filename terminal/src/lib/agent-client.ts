/** Agent REST + WebSocket base — browser uses same-origin rewrites in dev. */

const AGENT_DIRECT = "http://127.0.0.1:8080";

export function agentHttpBase(): string {
  const explicit = process.env.NEXT_PUBLIC_AGENT_URL?.replace(/\/$/, "");
  if (explicit) return explicit;
  if (typeof window !== "undefined") {
    // Next.js :3000 cannot proxy WebSocket upgrades — talk to agent API directly.
    if (window.location.port === "3000" || window.location.port === "3001") {
      return AGENT_DIRECT;
    }
    return window.location.origin;
  }
  return AGENT_DIRECT;
}

export function agentWsBase(): string {
  const explicit = process.env.NEXT_PUBLIC_AGENT_URL?.replace(/\/$/, "");
  const http = explicit || agentHttpBase();
  return http.replace(/^http/i, "ws");
}

export async function fetchAgentJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const url = `${agentHttpBase()}${path.startsWith("/") ? path : `/${path}`}`;
  const res = await fetch(url, {
    ...init,
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
  market_quotes?: Record<string, MarketQuote>;
  market_quotes_list?: MarketQuote[];
  ui_stress_render?: {
    active?: boolean;
    hz?: number;
    epic?: string;
    poll_ms?: number;
  };
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
  agent_pid?: number;
};
