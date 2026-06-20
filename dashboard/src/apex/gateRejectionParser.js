/**
 * Format lifecycle gate rejections for the CORE LIFE-CYCLE GATE REJECTION MONITOR.
 */

import { EPIC_TO_ASSET } from "./constants.js";

export const EXEC_CONFIDENCE_FLOOR = 45;
export const EXEC_FITNESS_FLOOR_PCT = 55;
export const ML_VETO_FLOOR = 0.45;

const GATE_LABELS = {
  session_open: "SESSION_CLOSED",
  session_blackout: "SESSION_BLACKOUT",
  cold_start_gap: "COLD_START_GAP",
  environment_fitness: "ENVIRONMENT_FITNESS_VETO",
  points_state: "POINTS_STATE_VETO",
  correlation_ok: "CORRELATION_VETO",
  risk_validation: "RISK_VALIDATION_VETO",
  expectancy_ok: "EXPECTANCY_VETO",
  calendar_ok: "CALENDAR_VETO",
  signal_confidence: "STRATEGY_CONFIDENCE_UNDER_FLOOR",
  ml_veto: "ML_VETO_FLOOR",
  execution: "EXECUTION_GATE_BLOCKED",
};

function asNum(v, fallback = null) {
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

/**
 * @param {Record<string, unknown>} gate
 * @returns {string | null}
 */
export function formatGateHoldLine(gate) {
  if (!gate || gate.pass) return null;
  const name = String(gate.name || "");
  const token = GATE_LABELS[name] || name.toUpperCase().replace(/\s+/g, "_");
  const value = gate.value;
  const detail = String(gate.detail || "").trim();

  if (name === "signal_confidence" && value && typeof value === "object") {
    const conf = asNum(value.confidence, 0);
    const req = asNum(value.threshold, EXEC_CONFIDENCE_FLOOR);
    return `HOLD: ${token} (Current ${conf.toFixed(1)}% vs Required ${req.toFixed(1)}%)`;
  }

  if (name === "environment_fitness" && value && typeof value === "object") {
    const score = asNum(value.score, 0);
    const min = asNum(value.min_required ?? value.min, EXEC_FITNESS_FLOOR_PCT);
    const scorePct = score <= 1 ? score * 100 : score;
    const minPct = min <= 1 ? min * 100 : min;
    return `HOLD: ${token} (Current ${(scorePct / 100).toFixed(3)} vs Required ${(minPct / 100).toFixed(3)} ML Floor)`;
  }

  if (name === "ml_veto" && value && typeof value === "object") {
    const prob = asNum(value.probability ?? value.p_win ?? value.score, 0);
    const floor = asNum(value.floor, ML_VETO_FLOOR);
    return `HOLD: ${token} (Current ${prob.toFixed(3)} vs Required ${floor.toFixed(3)})`;
  }

  if (name === "execution") {
    if (/rate|budget|rest/i.test(detail)) {
      return "HOLD: REST_BUDGET_EXHAUSTED";
    }
    return detail ? `HOLD: ${token} — ${detail}` : `HOLD: ${token}`;
  }

  if (detail) return `HOLD: ${token} — ${detail}`;
  return `HOLD: ${token}`;
}

/**
 * @param {import('./types.js').ParsedApexTelemetry | null} telemetry
 * @param {{ focusAssets?: string[] }} [opts]
 * @returns {Array<{ asset: string, epic: string, line: string, ts: string }>}
 */
export function extractGateRejections(telemetry, opts = {}) {
  if (!telemetry?.tick) return [];
  const focus = opts.focusAssets ?? ["GOLD", "WALL_STREET"];
  const out = [];
  const ts = String(telemetry.tick.ts || new Date().toISOString());

  for (const assetKey of focus) {
    const row = telemetry.assets?.[assetKey];
    if (!row) continue;
    let health = row.health ?? telemetry.tick.avionics_assets?.[assetKey]?.health;
    const markets = telemetry.tick.markets || {};
    if (!health && row.epic && markets[row.epic]?.health) {
      health = markets[row.epic].health;
    }
    const gates = health?.gates;
    if (!Array.isArray(gates)) {
      if (row.blocker) {
        out.push({
          asset: assetKey,
          epic: row.epic || assetKey,
          line: row.blocker.startsWith("HOLD:") ? row.blocker : `HOLD: ${row.blocker}`,
          ts,
        });
      }
      continue;
    }
    for (const gate of gates) {
      const line = formatGateHoldLine(gate);
      if (line) {
        out.push({ asset: assetKey, epic: row.epic || assetKey, line, ts });
      }
    }
    if (row.blocker && !out.some((r) => r.asset === assetKey)) {
      out.push({
        asset: assetKey,
        epic: row.epic || assetKey,
        line: row.blocker.startsWith("HOLD:") ? row.blocker : `HOLD: ${row.blocker}`,
        ts,
      });
    }
  }

  const markets = telemetry.tick.markets || {};
  for (const [epic, slice] of Object.entries(markets)) {
    if (typeof slice !== "object" || !slice) continue;
    const asset = EPIC_TO_ASSET[epic];
    if (!asset || focus.includes(asset)) continue;
    const health = slice.health;
    if (!health?.gates) continue;
    for (const gate of health.gates) {
      const line = formatGateHoldLine(gate);
      if (line) out.push({ asset, epic, line, ts });
    }
  }

  return out.slice(0, 24);
}
