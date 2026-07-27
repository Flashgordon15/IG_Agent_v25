/**
 * Per-port desk bootstrap — parallel REST for CFD :8080 and SB :8081.
 */

import {
  isDeskHealthOperational,
  type AgentHealth,
  type LivePositionsPayload,
} from "@/lib/agent-client";
import { fetchDeskJson } from "@/lib/desk-api-bases";
import {
  mapLivePositions,
  normalizeDeskMultiplex,
  type DeskMultiplexEnvelope,
  type EngineLaneId,
} from "@/lib/desk-multiplex";

export type PortHealthSnapshot = {
  online: boolean;
  healthOk: boolean;
  lastOkAt: number;
  quoteAgeMs: number | null;
  transport: string;
};

export type PortDeskSnapshot = {
  engineId: EngineLaneId;
  base: string;
  online: boolean;
  healthOk: boolean;
  envelope: DeskMultiplexEnvelope | null;
  brokerOpenSot: number | null;
  health: PortHealthSnapshot;
};

async function bootstrapPortRest(
  base: string,
  engineId: EngineLaneId,
  source: DeskMultiplexEnvelope["source"],
): Promise<PortDeskSnapshot> {
  const offline: PortDeskSnapshot = {
    engineId,
    base,
    online: false,
    healthOk: false,
    envelope: null,
    brokerOpenSot: null,
    health: {
      online: false,
      healthOk: false,
      lastOkAt: 0,
      quoteAgeMs: null,
      transport: "rest_poll",
    },
  };

  try {
    const [health, live, fulfillment, ops, state] = await Promise.all([
      fetchDeskJson<AgentHealth>(base, "/api/health", undefined, 2500).catch(
        () => null,
      ),
      fetchDeskJson<LivePositionsPayload>(
        base,
        "/api/positions/live",
        undefined,
        3500,
      ).catch(() => null),
      fetchDeskJson<Record<string, unknown>>(
        base,
        "/api/unified/fulfillment",
        undefined,
        2500,
      ).catch(() => ({})),
      fetchDeskJson<Record<string, unknown>>(
        base,
        "/api/desk/ops_strip",
        undefined,
        2500,
      ).catch(() => ({})),
      fetchDeskJson<Record<string, unknown>>(base, "/state", undefined, 2500).catch(
        () => ({}),
      ),
    ]);

    if (health == null) return offline;
    const healthOk = isDeskHealthOperational(health);
    if (!healthOk) return offline;

    const brokerOpenSot =
      live?.broker_open_sot?.count != null
        ? Number(live.broker_open_sot.count)
        : live?.count != null
          ? Number(live.count)
          : null;

    const merged: Record<string, unknown> = {
      ...state,
      ...fulfillment,
      mux: true,
      positions: Array.isArray(live?.positions)
        ? live.positions
        : (state as { positions?: unknown }).positions || [],
      quote_freshness:
        (fulfillment as { quote_freshness?: unknown }).quote_freshness ||
        (state as { quote_freshness?: unknown }).quote_freshness,
      gate_diagnostics:
        (fulfillment as { gate_diagnostics?: unknown }).gate_diagnostics ||
        (state as { gate_diagnostics?: unknown }).gate_diagnostics,
      all_ready:
        (fulfillment as { all_ready?: boolean }).all_ready ??
        (state as { all_ready?: boolean }).all_ready,
      // Health is SoT for A2 entry freeze; do not prefer fulfillment=false over health=true.
      trading_paused: (() => {
        const h = (health as { trading_paused?: boolean } | null)?.trading_paused;
        if (typeof h === "boolean") return h;
        const f = (fulfillment as { trading_paused?: boolean }).trading_paused;
        if (typeof f === "boolean") return f;
        const s = (state as { trading_paused?: boolean }).trading_paused;
        return typeof s === "boolean" ? s : undefined;
      })(),
      market_quotes: (fulfillment as { market_quotes?: unknown }).market_quotes,
      trading_path_live: (ops as { trading_path_live?: boolean }).trading_path_live,
      trading_path_badge: (ops as { trading_path_badge?: string }).trading_path_badge,
      rest_pressure: (ops as { rest_pressure?: boolean }).rest_pressure,
      rest_pressure_level: (ops as { rest_pressure_level?: string }).rest_pressure_level,
      entries_blocked:
        (ops as { trading_path_live?: boolean }).trading_path_live === false ||
        Boolean((ops as { rest_pressure?: boolean }).rest_pressure),
      engine_id: engineId,
    };

    let env = normalizeDeskMultiplex(merged, source);
    if (Array.isArray(live?.positions)) {
      env = { ...env, positions: mapLivePositions(live.positions) };
    }

    const now = performance.now();
    return {
      engineId,
      base,
      online: true,
      healthOk: true,
      envelope: env,
      brokerOpenSot,
      health: {
        online: true,
        healthOk: true,
        lastOkAt: now,
        quoteAgeMs: env.truth.quoteAgeMs,
        transport: env.feedTransport ?? "rest_poll",
      },
    };
  } catch {
    return offline;
  }
}

export async function bootstrapDualPorts(
  cfdBase: string,
  sbBase: string,
  source: DeskMultiplexEnvelope["source"],
): Promise<{ cfd: PortDeskSnapshot; sb: PortDeskSnapshot }> {
  const [cfd, sb] = await Promise.all([
    bootstrapPortRest(cfdBase, "cfd_sniper", source),
    bootstrapPortRest(sbBase, "sb_sentinel", source),
  ]);
  return { cfd, sb };
}
