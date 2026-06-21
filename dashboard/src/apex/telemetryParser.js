/**
 * Type-safe newline-delimited JSON tick parsing for Apex IPC stream.
 * @typedef {import('./types.js').ApexTickPayload} ApexTickPayload
 * @typedef {import('./types.js').AvionicsAssetTelemetry} AvionicsAssetTelemetry
 * @typedef {import('./types.js').PillarTelemetry} PillarTelemetry
 * @typedef {import('./types.js').ParsedApexTelemetry} ParsedApexTelemetry
 */

import {
  ASSET_KEYS,
  BASELINE_EQUITY_GBP,
  EPIC_TO_ASSET,
  ML_VETO_FLOOR,
  OPERATIONAL_PILLARS,
  PORTFOLIO_ENVELOPE_GBP,
} from "./constants.js";

function asNumber(value, fallback = null) {
  if (value == null || value === "") return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function asString(value, fallback = "") {
  if (value == null) return fallback;
  return String(value);
}

/**
 * @param {unknown} raw
 * @returns {ApexTickPayload | null}
 */
export function parseTickPayload(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  /** @type {Record<string, unknown>} */
  const obj = raw;
  return {
    type: asString(obj.type, "tick"),
    ts: asString(obj.ts, new Date().toISOString()),
    bid: asNumber(obj.bid),
    offer: asNumber(obj.offer),
    spread: asNumber(obj.spread),
    market_state: asString(obj.market_state),
    stream_status: asString(obj.stream_status),
    balance_gbp: asNumber(obj.balance_gbp),
    daily_pnl_gbp: asNumber(obj.daily_pnl_gbp),
    concurrent_risk_gbp: asNumber(obj.concurrent_risk_gbp),
    markets: typeof obj.markets === "object" && obj.markets ? obj.markets : {},
    avionics_assets:
      typeof obj.avionics_assets === "object" && obj.avionics_assets
        ? obj.avionics_assets
        : {},
    avionics_hud:
      typeof obj.avionics_hud === "object" && obj.avionics_hud ? obj.avionics_hud : {},
    health: typeof obj.health === "object" && obj.health ? obj.health : null,
    config: typeof obj.config === "object" && obj.config ? obj.config : null,
    points: typeof obj.points === "object" && obj.points ? obj.points : null,
    trading_healthy: obj.trading_healthy,
    quotes_fresh: obj.quotes_fresh,
    agent_pid: asNumber(obj.agent_pid),
    raw: obj,
  };
}

function confidenceFromSlice(slice) {
  if (!slice || typeof slice !== "object") return null;
  const health = slice.health;
  if (health && typeof health === "object" && Array.isArray(health.gates)) {
    for (const gate of health.gates) {
      if (!gate || gate.name !== "signal_confidence") continue;
      const val = gate.value;
      if (val && typeof val === "object") {
        const c = asNumber(val.confidence ?? val.rules_confidence);
        if (c != null) return c;
      }
    }
  }
  const signal = slice.signal;
  if (signal && typeof signal === "object") {
    return asNumber(signal.confidence ?? signal.rules_confidence ?? signal.signal_core_score);
  }
  return asNumber(slice.confidence ?? slice.signal_confidence);
}

function rsiFromSlice(slice) {
  if (!slice || typeof slice !== "object") return null;
  const signal = slice.signal;
  if (signal && typeof signal === "object") {
    const r = asNumber(signal.rsi);
    if (r != null) return r;
  }
  return asNumber(slice.rsi);
}

function fitnessFromSlice(slice) {
  if (!slice || typeof slice !== "object") return null;
  const health = slice.health;
  if (health && typeof health === "object" && Array.isArray(health.gates)) {
    for (const gate of health.gates) {
      if (!gate || gate.name !== "environment_fitness") continue;
      const val = gate.value;
      if (val && typeof val === "object" && val.score != null) {
        return asNumber(val.score);
      }
    }
  }
  return asNumber(slice.fitness ?? slice.environment_fitness);
}

function volatilityFromHistory(mid, prevMid) {
  if (mid == null || prevMid == null || prevMid <= 0) return null;
  return Math.abs((mid - prevMid) / prevMid);
}

/** @type {Map<string, number>} */
const _lastMidByAsset = new Map();

function midFromSlice(slice) {
  if (!slice || typeof slice !== "object") return null;
  const bid = asNumber(slice.bid);
  const offer = asNumber(slice.offer);
  if (bid != null && offer != null && bid > 0 && offer > 0) {
    return (bid + offer) / 2;
  }
  return asNumber(slice.mid ?? slice.price);
}

/**
 * @param {ApexTickPayload} tick
 * @param {string} assetKey
 * @returns {AvionicsAssetTelemetry}
 */
export function extractAssetTelemetry(tick, assetKey) {
  const avionics = tick.avionics_assets?.[assetKey];
  const hud = tick.avionics_hud?.[assetKey];
  const topLevel = tick.raw?.[assetKey];
  const markets = tick.markets || {};

  let slice = null;
  if (avionics && typeof avionics === "object") slice = avionics;
  else if (hud && typeof hud === "object") slice = hud;
  else if (topLevel && typeof topLevel === "object") slice = topLevel;
  else {
    const epic = Object.entries(EPIC_TO_ASSET).find(([, k]) => k === assetKey)?.[0];
    if (epic && markets[epic] && typeof markets[epic] === "object") {
      slice = markets[epic];
    } else if (markets[assetKey] && typeof markets[assetKey] === "object") {
      slice = markets[assetKey];
    }
  }

  const signal = slice?.signal && typeof slice.signal === "object" ? slice.signal : {};
  const confidence = confidenceFromSlice(slice) ?? asNumber(hud?.confidence);
  const rsi = rsiFromSlice(slice) ?? asNumber(hud?.rsi);
  const mid = midFromSlice(slice);
  const prevMid = _lastMidByAsset.get(assetKey);
  const volatility = mid != null && prevMid != null ? volatilityFromHistory(mid, prevMid) : null;
  if (mid != null) _lastMidByAsset.set(assetKey, mid);
  const fitness = fitnessFromSlice(slice) ?? asNumber(hud?.fitness);
  const epicKey = asString(
    slice?.epic ??
      avionics?.epic ??
      Object.entries(EPIC_TO_ASSET).find(([, k]) => k === assetKey)?.[0] ??
      "",
  );
  let health =
    (slice?.health && typeof slice.health === "object" ? slice.health : null) ??
    (hud?.health && typeof hud.health === "object" ? hud.health : null);
  if (!health && epicKey && markets[epicKey]?.health) {
    health = markets[epicKey].health;
  }

  return {
    assetKey,
    epic: epicKey,
    mid,
    bid: asNumber(slice?.bid),
    offer: asNumber(slice?.offer),
    spread: asNumber(slice?.spread),
    confidence,
    rsi,
    fitness,
    volatility,
    health,
    direction: asString(signal.direction ?? slice?.direction ?? "WAIT"),
    marketState: asString(slice?.market_state ?? tick.market_state ?? "—"),
    streamStatus: asString(slice?.stream_status ?? tick.stream_status ?? "—"),
    blocker: asString(signal.block_reason ?? slice?.block_reason ?? ""),
  };
}

function mlProbabilityFromTick(tick) {
  const health = tick.health;
  if (health && typeof health === "object" && Array.isArray(health.gates)) {
    for (const gate of health.gates) {
      if (!gate || gate.name !== "ml_veto") continue;
      const val = gate.value;
      if (val && typeof val === "object") {
        return asNumber(val.probability ?? val.p_win ?? val.score);
      }
    }
  }
  return null;
}

function pillarStatus(ok, degraded = false) {
  if (ok) return "active";
  if (degraded) return "degraded";
  return "blocked";
}

/**
 * @param {ApexTickPayload} tick
 * @returns {PillarTelemetry}
 */
export function extractPillarTelemetry(tick) {
  const concurrent = asNumber(tick.concurrent_risk_gbp, 0) ?? 0;
  const baseline =
    asNumber(tick.config?.simulated_equity_gbp) ??
    asNumber(tick.raw?.simulated_equity_gbp) ??
    BASELINE_EQUITY_GBP;
  const envelopePct = PORTFOLIO_ENVELOPE_GBP > 0 ? (concurrent / PORTFOLIO_ENVELOPE_GBP) * 100 : 0;
  const mlProb = mlProbabilityFromTick(tick);
  const mlFloor = ML_VETO_FLOOR;
  const mlUnblocked = mlProb == null || mlProb >= mlFloor;

  const tradingHealthy = tick.trading_healthy !== false;
  const quotesFresh = tick.quotes_fresh !== false;
  const pointsState = asString(tick.points?.state, "—");
  const streamLive = asString(tick.stream_status).toUpperCase() === "LIVE";

  const pillars = OPERATIONAL_PILLARS.map((p) => {
    let status = "active";
    let detail = "nominal";
    switch (p.id) {
      case "A":
        status = pillarStatus(pointsState !== "STOP");
        detail = `points ${pointsState}`;
        break;
      case "B":
        status = pillarStatus(streamLive, !quotesFresh);
        detail = tick.stream_status || "—";
        break;
      case "C":
        status = pillarStatus(tradingHealthy);
        detail = tradingHealthy ? "loops healthy" : "degraded";
        break;
      case "D":
        status = pillarStatus(concurrent <= PORTFOLIO_ENVELOPE_GBP, concurrent > PORTFOLIO_ENVELOPE_GBP * 0.85);
        detail = `£${concurrent.toFixed(0)} / £${PORTFOLIO_ENVELOPE_GBP}`;
        break;
      case "E":
        status = pillarStatus(quotesFresh, !quotesFresh);
        detail = quotesFresh ? "quotes fresh" : "stale feed";
        break;
      default:
        break;
    }
    return { ...p, status, detail };
  });

  return {
    baselineEquityGbp: baseline,
    portfolioEnvelopeGbp: PORTFOLIO_ENVELOPE_GBP,
    concurrentRiskGbp: concurrent,
    envelopeUtilPct: envelopePct,
    mlVetoFloor: mlFloor,
    mlProbability: mlProb,
    mlUnblocked,
    pillars,
  };
}

/**
 * Parse schema 1.1 dual-track SHM envelope for 4-Pillar cockpit.
 * @param {unknown} raw
 * @returns {{ live: ApexTickPayload | null, shadow: ApexTickPayload | null, envelope: Record<string, unknown> | null }}
 */
export function parseDualTrackEnvelope(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { live: null, shadow: null, envelope: null };
  }
  const obj = /** @type {Record<string, unknown>} */ (raw);
  if (obj.schema_version !== "1.1" || !Array.isArray(obj.streams)) {
    return { live: null, shadow: null, envelope: null };
  }
  let live = null;
  let shadow = null;
  for (const stream of obj.streams) {
    if (!stream || typeof stream !== "object") continue;
    const row = /** @type {Record<string, unknown>} */ (stream);
    const prefix = String(row.prefix || "");
    const payload = parseTickPayload(row.payload);
    if (!payload) continue;
    if (prefix.includes("LIVE-TRACK")) {
      live = payload;
    } else if (prefix.includes("MOCK-TRACK")) {
      shadow = payload;
    }
  }
  return { live, shadow, envelope: obj };
}

/**
 * @param {unknown} raw
 * @returns {ParsedApexTelemetry | null}
 */
export function parseApexTelemetry(raw) {
  const dual = parseDualTrackEnvelope(raw);
  const tick = dual.live || parseTickPayload(raw);
  if (!tick) return null;

  const assets = {};
  for (const key of ASSET_KEYS) {
    assets[key] = extractAssetTelemetry(tick, key);
  }

  return {
    tick,
    receivedAt: Date.now(),
    assets,
    pillars: extractPillarTelemetry(tick),
    transparency: extractOperationalTransparency(tick),
  };
}

/**
 * @param {ApexTickPayload} tick
 * @returns {import('./types.js').OperationalTransparency | null}
 */
export function extractOperationalTransparency(tick) {
  const raw =
    tick.raw?.operational_transparency ??
    tick.raw?.operationalTransparency ??
    null;
  if (!raw || typeof raw !== "object") return null;
  return /** @type {import('./types.js').OperationalTransparency} */ (raw);
}
