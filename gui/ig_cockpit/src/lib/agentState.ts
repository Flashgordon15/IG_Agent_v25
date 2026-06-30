import type { JsonObject } from "../types/cockpit";

/** Overlay live /api/state fields onto gui_status for mappers (non-blocking telemetry). */
export function mergeAgentStateIntoGui(
  gui: JsonObject | null,
  agent: JsonObject | null,
): JsonObject | null {
  if (!agent) return gui;
  const base = { ...(gui ?? {}) };
  const feeds = agent.feeds;
  const routing = agent.routing;
  const risk = agent.risk_envelope;
  const pipeline = agent.pipeline;
  const flags = agent.governance_flags;

  if (Array.isArray(feeds) && feeds.length) {
    base.api_feed_health = feeds;
  }
  if (Array.isArray(routing) && routing.length) {
    base.unified_execution_route = routing;
  }
  if (Array.isArray(risk) && risk.length) {
    base.regime_risk_envelope = risk;
  }
  if (Array.isArray(pipeline) && pipeline.length) {
    base.trade_pipeline_health = pipeline;
  }
  if (agent.session_id) {
    base.session_id = agent.session_id;
  }
  if (agent.session_status) {
    base.session_status = agent.session_status;
  }
  if (agent.account_scope) {
    base.account_scope = agent.account_scope;
  }
  if (agent.gate_progression) {
    base.gate_progression = agent.gate_progression;
  }
  if (agent.readiness_level !== undefined) {
    base.readiness_level = agent.readiness_level;
  }
  if (agent.readiness_label) {
    base.readiness_label = agent.readiness_label;
  }
  if (agent.subsystem_readiness) {
    base.subsystem_readiness = agent.subsystem_readiness;
  }
  if (agent.cockpit_usable !== undefined) {
    base.cockpit_usable = agent.cockpit_usable;
  }
  if (agent.partial_ready !== undefined) {
    base.partial_ready = agent.partial_ready;
  }
  if (agent.trading_ready !== undefined) {
    base.trading_ready = agent.trading_ready;
  }
  if (Array.isArray(flags) && flags.length) {
    const hard = flags.filter((f) => f.type === "hard_enforcement");
    if (hard.length) {
      base.hard_enforcement_decisions = hard.map((f) => ({
        epic: f.epic,
        active: true,
        reason: f.reason,
      }));
    }
    const gov = flags.find((f) => f.type === "pipeline_governance");
    if (gov) {
      base.pipeline_governance = {
        ...(base.pipeline_governance as JsonObject),
        risk_posture: gov.reason,
      };
    }
  }
  base.agent_state_version = agent.version;
  base.agent_state_updated_at = agent.updated_at;
  return base;
}

export function routingFromAgentState(agent: JsonObject | null): JsonObject | null {
  if (!agent) return null;
  return {
    source: "agent_state",
    unified_execution_route: agent.routing ?? [],
    ts: agent.updated_at,
  };
}

export function riskFromAgentState(agent: JsonObject | null): JsonObject | null {
  if (!agent) return null;
  return {
    source: "agent_state",
    regime_risk_envelope: agent.risk_envelope ?? [],
    pipeline_governance: agent.governance_flags ?? [],
    hard_enforcement_decisions: (
      Array.isArray(agent.governance_flags) ? agent.governance_flags : []
    ).filter((f: JsonObject) => f.type === "hard_enforcement"),
    ts: agent.updated_at,
  };
}

/** Partial readiness — agent state is always available; full gui_status still needed for splash. */
export function isAgentStateLive(agent: JsonObject | null): boolean {
  if (!agent) return false;
  const version = Number(agent.version ?? 0);
  return version > 0;
}
