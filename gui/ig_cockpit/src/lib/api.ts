import { invoke } from "@tauri-apps/api/core";

export const API_BASE =
  import.meta.env.VITE_IG_AGENT_API_URL ?? "http://127.0.0.1:8080";

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function fetchJson<T = unknown>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new Error(`${path}: HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function getGuiStatus(): Promise<Record<string, unknown>> {
  if (isTauri()) {
    return invoke<Record<string, unknown>>("get_gui_status");
  }
  return fetchJson("/api/gui_status");
}

export async function getAgentState(): Promise<Record<string, unknown>> {
  if (isTauri()) {
    return invoke<Record<string, unknown>>("get_agent_state");
  }
  return fetchJson("/api/state");
}

export async function getPnlData(): Promise<Record<string, unknown>> {
  if (isTauri()) {
    return invoke<Record<string, unknown>>("get_pnl_data");
  }
  try {
    return await fetchJson("/api/pnl");
  } catch {
    const [state, gui] = await Promise.all([
      fetchJson<Record<string, unknown>>("/state"),
      fetchJson<Record<string, unknown>>("/api/gui_status"),
    ]);
    return {
      source: "derived",
      daily_pnl_gbp: state.daily_pnl_gbp,
      balance_gbp: state.balance_gbp,
      points: state.points,
      daily_pnl_targeting: gui.daily_pnl_targeting,
      session_review: gui.session_review,
      ts: gui.ts ?? state.ts,
    };
  }
}

export async function getRoutingMetrics(): Promise<Record<string, unknown>> {
  if (isTauri()) {
    return invoke<Record<string, unknown>>("get_routing_metrics");
  }
  try {
    const agent = await fetchJson<Record<string, unknown>>("/api/state");
    if (Array.isArray(agent.routing) && agent.routing.length) {
      return {
        source: "agent_state",
        unified_execution_route: agent.routing,
        ts: agent.updated_at,
      };
    }
  } catch {
    /* fall through */
  }
  try {
    return await fetchJson("/api/routing");
  } catch {
    const gui = await fetchJson<Record<string, unknown>>("/api/gui_status");
    return {
      source: "derived",
      unified_execution_route: gui.unified_execution_route,
      strategy_controller_decisions: gui.strategy_controller_decisions,
      hard_enforcement_decisions: gui.hard_enforcement_decisions,
      ts: gui.ts,
    };
  }
}

export async function getRiskState(): Promise<Record<string, unknown>> {
  if (isTauri()) {
    return invoke<Record<string, unknown>>("get_risk_state");
  }
  try {
    const agent = await fetchJson<Record<string, unknown>>("/api/state");
    if (Array.isArray(agent.risk_envelope) || Array.isArray(agent.governance_flags)) {
      return {
        source: "agent_state",
        regime_risk_envelope: agent.risk_envelope,
        governance_flags: agent.governance_flags,
        ts: agent.updated_at,
      };
    }
  } catch {
    /* fall through */
  }
  try {
    return await fetchJson("/api/risk");
  } catch {
    const gui = await fetchJson<Record<string, unknown>>("/api/gui_status");
    return {
      source: "derived",
      regime_risk_envelope: gui.regime_risk_envelope,
      regime_sizing_advice: gui.regime_sizing_advice,
      pipeline_governance: gui.pipeline_governance,
      session_governance: gui.session_governance,
      daily_pnl_targeting: gui.daily_pnl_targeting,
      hard_enforcement_decisions: gui.hard_enforcement_decisions,
      ts: gui.ts,
    };
  }
}

export async function getLogs(): Promise<Record<string, unknown>> {
  if (isTauri()) {
    return invoke<Record<string, unknown>>("get_logs");
  }
  try {
    return await fetchJson("/api/logs");
  } catch {
    const [signals, state] = await Promise.all([
      fetchJson<Record<string, unknown>>("/api/signals?limit=50").catch(
        () => ({ signals: [] }),
      ),
      fetchJson<Record<string, unknown>>("/state").catch(
        () => ({}) as Record<string, unknown>,
      ),
    ]);
    return {
      source: "derived",
      signals: signals.signals ?? [],
      errors: state.errors,
      health_summary: (state.health as Record<string, unknown> | undefined)
        ?.summary,
      ts: state.ts,
    };
  }
}

export async function fetchBootStatus(): Promise<Record<string, unknown>> {
  return fetchJson("/api/boot_status");
}

export async function fetchBootLog(limit = 50): Promise<Record<string, unknown>> {
  return fetchJson(`/api/boot_log?limit=${limit}`);
}

export async function fetchHealthLight(): Promise<Record<string, unknown>> {
  if (isTauri()) {
    try {
      return await invoke<Record<string, unknown>>("get_health_light");
    } catch {
      /* fall through to HTTP */
    }
  }
  return fetchJson("/api/health_light");
}

export async function fetchUnifiedStatus(): Promise<Record<string, unknown>> {
  return fetchJson("/api/unified_status");
}

export async function fetchTradeLifecycle(): Promise<Record<string, unknown>> {
  return fetchJson("/api/trade_lifecycle");
}

export async function fetchRejections(limit = 20): Promise<Record<string, unknown>> {
  return fetchJson(`/api/rejections?limit=${limit}`);
}

export async function fetchRotationStatus(): Promise<Record<string, unknown>> {
  return fetchJson("/api/rotation_status");
}

export async function fetchTradeState(): Promise<Record<string, unknown>> {
  return fetchJson("/api/trade_state");
}

export async function fetchTradeEvents(limit = 50): Promise<Record<string, unknown>> {
  return fetchJson(`/api/trade_events?limit=${limit}`);
}

export async function fetchRotationState(): Promise<Record<string, unknown>> {
  return fetchJson("/api/rotation_state");
}

export async function fetchIgBudgetState(): Promise<Record<string, unknown>> {
  return fetchJson("/api/ig_budget_state");
}

export function wsStreamUrl(): string {
  const base = API_BASE.replace(/^https:/, "wss:").replace(/^http:/, "ws:");
  return `${base}/ws/stream`;
}

export function wsStateUrl(): string {
  const base = API_BASE.replace(/^https:/, "wss:").replace(/^http:/, "ws:");
  return `${base}/ws/state`;
}

async function postJson<T = Record<string, unknown>>(
  path: string,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "POST" });
  if (!res.ok) {
    throw new Error(`${path}: HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/** Non-blocking agent control — fire-and-forget safe for live trading UI. */
export function pauseTrading(): void {
  void postJson("/api/stop").catch(() => undefined);
}

export function resumeTrading(): void {
  void postJson("/api/start").catch(() => undefined);
}

export function suppressOrders(): void {
  void postJson("/api/emergency_stop").catch(() => undefined);
}

export function unblockOperational(): void {
  void postJson("/api/operational/unblock").catch(() => undefined);
}

