/**
 * Multiplex desk bus — normalizes agent WS / bootstrap REST into one RAM envelope.
 * Channels: ticks | positions | arms | session_pnl (+ execution truth).
 */

import { EPIC_LABELS } from "@/lib/constants";
import type { LivePositionRow } from "@/lib/agent-client";
import type { GpuExecPosition } from "@/lib/gpu-execution-buffer";

/** Lightstreamer / WS sniper ceiling (ms). */
export const LS_QUOTE_CEILING_MS = 500;
/** REST poll / Yahoo desk budget (ms) — aligns with backend ~10s entry veto. */
export const REST_POLL_QUOTE_CEILING_MS = 10_000;
/** v33 telemetry-loss badge — feed freeze regardless of adaptive REST budget. */
export const TELEMETRY_FREEZE_MS = 5000;
/** Boot splash / harness — relax trade_support SoT during first 60s. */
export const BOOT_SOT_GRACE_SEC = 60;
/** Relaxed ops-badge latency buffer during boot hydration (matches harness config). */
export const BOOT_LATENCY_BUFFER_SEC = 30;
export const BOOT_SOT_STALE_BUDGET_SEC = 30;
export const RUNTIME_SOT_STALE_SEC = 20;
/** @deprecated use resolveQuoteBudgetMs — kept for imports that expect a constant */
export const QUOTE_AGE_CEILING_MS = LS_QUOTE_CEILING_MS;

export function tradeSupportStaleBudgetSec(
  bootStartedAtSec?: number | null,
  serverBudgetSec?: number | null,
): number {
  if (serverBudgetSec != null && Number.isFinite(Number(serverBudgetSec))) {
    return Number(serverBudgetSec);
  }
  if (bootStartedAtSec != null && Number.isFinite(Number(bootStartedAtSec))) {
    const raw = Number(bootStartedAtSec);
    const startedMs = raw > 1e12 ? raw : raw * 1000;
    const elapsedSec = (Date.now() - startedMs) / 1000;
    if (elapsedSec < BOOT_SOT_GRACE_SEC) return BOOT_SOT_STALE_BUDGET_SEC;
  }
  return RUNTIME_SOT_STALE_SEC;
}

/**
 * Telemetry freeze threshold — 30s during boot hydration, 5s steady-state.
 * Prevents false ENGINE BLOCKAGE / DESK TRADING DOWN while sockets settle.
 */
export function resolveTelemetryFreezeMs(
  bootStartedAtSec?: number | null,
  serverBufferSec?: number | null,
): number {
  const bufferSec =
    serverBufferSec != null && Number.isFinite(Number(serverBufferSec))
      ? Number(serverBufferSec)
      : BOOT_LATENCY_BUFFER_SEC;
  if (bootStartedAtSec != null && Number.isFinite(Number(bootStartedAtSec))) {
    const raw = Number(bootStartedAtSec);
    const startedMs = raw > 1e12 ? raw : raw * 1000;
    const elapsedSec = (Date.now() - startedMs) / 1000;
    if (elapsedSec < bufferSec) return Math.max(TELEMETRY_FREEZE_MS, bufferSec * 1000);
  }
  return TELEMETRY_FREEZE_MS;
}

export function isBootLatencyBufferActive(
  bootStartedAtSec?: number | null,
  serverBufferSec?: number | null,
): boolean {
  const bufferSec =
    serverBufferSec != null && Number.isFinite(Number(serverBufferSec))
      ? Number(serverBufferSec)
      : BOOT_LATENCY_BUFFER_SEC;
  if (bootStartedAtSec == null || !Number.isFinite(Number(bootStartedAtSec))) {
    return false;
  }
  const raw = Number(bootStartedAtSec);
  const startedMs = raw > 1e12 ? raw : raw * 1000;
  return (Date.now() - startedMs) / 1000 < bufferSec;
}

export function isTradeSupportSotFresh(
  statusAgeSec: number | null | undefined,
  bootStartedAtSec?: number | null,
  serverBudgetSec?: number | null,
): boolean {
  if (statusAgeSec == null || !Number.isFinite(Number(statusAgeSec))) {
    return false;
  }
  return Number(statusAgeSec) < tradeSupportStaleBudgetSec(
    bootStartedAtSec,
    serverBudgetSec,
  );
}

export type EngineLaneId = "cfd_sniper" | "sb_sentinel";

export type DeskEngineLane = {
  engineId: EngineLaneId;
  label: string;
  accountId: string;
  productType: string;
  engineOrigin: string;
  quoteAgeMs: number | null;
  quoteBudgetMs: number;
  transport: string;
  pathLive: boolean | null;
  operational: boolean;
  standby: boolean;
  openCount: number;
  positions: GpuExecPosition[];
};

export type SniperArmState = "ACTIVE" | "SUPPRESSED" | "VETOED";

export type DeskTickChannel = {
  epic: string;
  mid: number;
  bid: number;
  offer: number;
  ageMs: number | null;
};

export type DeskArmsChannel = {
  sniper: SniperArmState;
  allReady: boolean | null;
  tradingPaused: boolean | null;
  reason: string;
};

export type DeskSessionPnlChannel = {
  realizedGbp: number;
  unrealizedGbp: number;
  dailyGbp: number;
};

export type DeskExecutionTruth = {
  quoteAgeMs: number | null;
  sniperArm: SniperArmState;
  gateVerdict: string;
  updatedAt: number;
};

export type DeskMultiplexEnvelope = {
  mux: true;
  ticks: DeskTickChannel[];
  positions: GpuExecPosition[];
  arms: DeskArmsChannel;
  session_pnl: DeskSessionPnlChannel;
  truth: DeskExecutionTruth;
  source: "ws" | "bootstrap" | "recovery";
  engines?: DeskEngineLane[];
  feedTransport?: string;
  core_detached?: boolean;
};

const DOW = "IX.D.DOW.IFM.IP";

const LANE_DEFAULTS: Record<
  EngineLaneId,
  { label: string; accountId: string; productType: string; engineOrigin: string }
> = {
  cfd_sniper: {
    label: "QUANT SNIPER (CFD - Z6BAH4)",
    accountId: "Z6BAH4",
    productType: "CFD",
    engineOrigin: "QUANT_SNIPER",
  },
  sb_sentinel: {
    label: "MACRO SENTINEL (SB - Z6BAH3)",
    accountId: "Z6BAH3",
    productType: "SPREADBET",
    engineOrigin: "MACRO_SENTINEL",
  },
};

export function resolveFeedTransport(msg: Record<string, unknown>): string {
  const direct =
    msg.feed_transport ??
    msg.streaming_transport ??
    (msg.feed_transport_summary as { streaming_transport?: string } | undefined)
      ?.streaming_transport;
  if (typeof direct === "string" && direct.trim()) return direct.trim().toLowerCase();
  const qf = msg.quote_freshness as { transport?: string } | undefined;
  if (qf?.transport) return String(qf.transport).toLowerCase();
  return "rest_poll";
}

/** Adaptive quote-age budget from transport (LS strict 500ms; REST/Yahoo 10s). */
export function resolveQuoteBudgetMs(transport?: string | null): number {
  const t = String(transport || "rest_poll").toLowerCase();
  if (
    t.includes("lightstreamer") ||
    t === "ws" ||
    t === "stream" ||
    t === "ls"
  ) {
    return LS_QUOTE_CEILING_MS;
  }
  if (t.includes("rest") || t.includes("yahoo") || t.includes("poll")) {
    return REST_POLL_QUOTE_CEILING_MS;
  }
  return REST_POLL_QUOTE_CEILING_MS;
}

export function quoteWithinBudget(
  ageMs: number | null | undefined,
  budgetMs: number,
): boolean {
  if (ageMs == null || !Number.isFinite(Number(ageMs))) return false;
  return Number(ageMs) <= budgetMs;
}

export function laneOperational(
  lane: Pick<DeskEngineLane, "quoteAgeMs" | "quoteBudgetMs" | "standby" | "pathLive">,
): boolean {
  if (lane.standby) return true;
  if (lane.pathLive === false) return false;
  return quoteWithinBudget(lane.quoteAgeMs, lane.quoteBudgetMs);
}

export function dualEngineOperational(lanes: DeskEngineLane[]): boolean {
  if (!lanes.length) return false;
  return lanes.every((lane) => laneOperational(lane));
}

export type DualPortHealth = {
  cfd: { online: boolean; healthOk: boolean; quoteAgeMs: number | null };
  sb: { online: boolean; healthOk: boolean; quoteAgeMs: number | null };
};

/** Emerald dual badge — both ports reachable and each lane within adaptive quote budget. */
export function dualPortOperational(
  portHealth: DualPortHealth,
  lanes: DeskEngineLane[],
): boolean {
  if (!portHealth.cfd.online || !portHealth.sb.online) return false;
  if (!portHealth.cfd.healthOk || !portHealth.sb.healthOk) return false;
  if (lanes.length < 2) return false;
  return dualEngineOperational(lanes);
}

export function buildDualPortLanes(
  cfd: {
    online: boolean;
    healthOk: boolean;
    envelope: DeskMultiplexEnvelope | null;
    transport?: string;
  },
  sb: {
    online: boolean;
    healthOk: boolean;
    envelope: DeskMultiplexEnvelope | null;
    transport?: string;
  },
): DeskEngineLane[] {
  const mkLane = (
    snap: typeof cfd,
    engineId: EngineLaneId,
  ): DeskEngineLane => {
    const defs = LANE_DEFAULTS[engineId];
    const env = snap.envelope;
    const transport =
      snap.transport || env?.feedTransport || resolveFeedTransport({});
    const budget = resolveQuoteBudgetMs(transport);
    const ageMs = env?.truth.quoteAgeMs ?? null;
    const positions = env?.positions ?? [];
    const pathLive = snap.online && snap.healthOk;
    const lane: DeskEngineLane = {
      engineId,
      label: defs.label,
      accountId: defs.accountId,
      productType: defs.productType,
      engineOrigin: defs.engineOrigin,
      quoteAgeMs: ageMs,
      quoteBudgetMs: budget,
      transport,
      pathLive,
      standby: false,
      openCount: positions.length,
      positions,
      operational: false,
    };
    lane.operational = laneOperational(lane);
    return lane;
  };

  return [mkLane(cfd, "cfd_sniper"), mkLane(sb, "sb_sentinel")];
}

export function mergeDualPortEnvelope(
  cfd: DeskMultiplexEnvelope | null,
  sb: DeskMultiplexEnvelope | null,
  source: DeskMultiplexEnvelope["source"],
): DeskMultiplexEnvelope {
  const primary = cfd ?? sb;
  if (!primary) {
    return normalizeDeskMultiplex({}, source);
  }

  const positions = [...(cfd?.positions ?? []), ...(sb?.positions ?? [])];
  const ticks = [...(cfd?.ticks ?? []), ...(sb?.ticks ?? [])];
  const tickMap = new Map<string, DeskTickChannel>();
  for (const t of ticks) tickMap.set(t.epic, t);

  const realized =
    (cfd?.session_pnl.realizedGbp ?? 0) + (sb?.session_pnl.realizedGbp ?? 0);
  const unrealized =
    (cfd?.session_pnl.unrealizedGbp ?? 0) + (sb?.session_pnl.unrealizedGbp ?? 0);
  const daily =
    (cfd?.session_pnl.dailyGbp ?? 0) + (sb?.session_pnl.dailyGbp ?? 0);

  return {
    ...primary,
    mux: true,
    ticks: [...tickMap.values()],
    positions,
    session_pnl: {
      realizedGbp: realized,
      unrealizedGbp: unrealized,
      dailyGbp: daily,
    },
    source,
  };
}

function buildEngineLanes(
  msg: Record<string, unknown>,
  positions: GpuExecPosition[],
  ageMs: number | null,
  transport: string,
): DeskEngineLane[] {
  const budget = resolveQuoteBudgetMs(transport);
  const pathLive =
    msg.trading_path_live != null ? Boolean(msg.trading_path_live) : null;
  const rawEngines = msg.engines;
  if (Array.isArray(rawEngines) && rawEngines.length >= 2) {
    return rawEngines.slice(0, 2).map((raw, idx) => {
      const row = raw as Record<string, unknown>;
      const engineId = (
        String(row.engine_id || row.engineId || (idx === 0 ? "cfd_sniper" : "sb_sentinel"))
      ) as EngineLaneId;
      const defs = LANE_DEFAULTS[engineId] || LANE_DEFAULTS.sb_sentinel;
      const laneAge =
        row.quote_age_ms != null
          ? Number(row.quote_age_ms)
          : row.quoteAgeMs != null
            ? Number(row.quoteAgeMs)
            : ageMs;
      const laneBudget = resolveQuoteBudgetMs(
        String(row.transport || row.streaming_transport || transport),
      );
      const lanePositions = Array.isArray(row.positions)
        ? mapStatePositions(row.positions)
        : positions.filter((p) =>
            String(row.product_type || defs.productType).toUpperCase() === "CFD"
              ? p.epic.includes("CFD")
              : !p.epic.includes("CFD"),
          );
      const standby = Boolean(row.standby);
      const lanePath =
        row.path_live != null
          ? Boolean(row.path_live)
          : row.pathLive != null
            ? Boolean(row.pathLive)
            : pathLive;
      const lane: DeskEngineLane = {
        engineId,
        label: String(row.label || defs.label),
        accountId: String(row.account_id || row.accountId || defs.accountId),
        productType: String(row.product_type || row.productType || defs.productType),
        engineOrigin: String(row.engine_origin || row.engineOrigin || defs.engineOrigin),
        quoteAgeMs: laneAge,
        quoteBudgetMs: laneBudget,
        transport: String(row.transport || transport),
        pathLive: lanePath,
        standby,
        openCount: Number(row.open_count ?? row.openCount ?? lanePositions.length) || 0,
        positions: lanePositions,
        operational: false,
      };
      lane.operational = laneOperational(lane);
      return lane;
    });
  }

  const product = String(
    msg.broker_account_product ??
      (msg.dual_core as { broker_account_product?: string } | undefined)
        ?.broker_account_product ??
      "SPREADBET",
  ).toUpperCase();
  const liveId: EngineLaneId = product === "CFD" ? "cfd_sniper" : "sb_sentinel";
  const standbyId: EngineLaneId = liveId === "cfd_sniper" ? "sb_sentinel" : "cfd_sniper";

  const liveLane: DeskEngineLane = {
    ...LANE_DEFAULTS[liveId],
    engineId: liveId,
    quoteAgeMs: ageMs,
    quoteBudgetMs: budget,
    transport,
    pathLive,
    standby: false,
    openCount: positions.length,
    positions,
    operational: false,
  };
  liveLane.operational = laneOperational(liveLane);

  const standbyLane: DeskEngineLane = {
    ...LANE_DEFAULTS[standbyId],
    engineId: standbyId,
    quoteAgeMs: null,
    quoteBudgetMs: budget,
    transport,
    pathLive: null,
    standby: true,
    openCount: 0,
    positions: [],
    operational: true,
  };

  return liveId === "cfd_sniper" ? [liveLane, standbyLane] : [standbyLane, liveLane];
}

function labelFor(epic: string): string {
  return EPIC_LABELS[epic] || epic.split(".")[2] || epic;
}

export function mapLivePositions(rows: LivePositionRow[]): GpuExecPosition[] {
  return rows
    .filter((p) => p?.deal_id)
    .map((p) => {
      const soft =
        p.protection_summary?.soft_loss_gbp ?? p.soft_loss_gbp ?? null;
      const trail =
        p.protection_summary?.trail_floor_gbp ?? p.trail_floor_gbp ?? null;
      return {
        dealId: p.deal_id,
        epic: String(p.epic || ""),
        label: labelFor(String(p.epic || "")),
        direction: String(p.direction || "BUY").toUpperCase(),
        size: Number(p.size ?? 0.5) || 0.5,
        entry: Number(p.entry ?? 0) || 0,
        softLossGbp: soft != null ? Number(soft) : null,
        trailFloorGbp: trail != null ? Number(trail) : null,
        pnlGbp: p.pnl_gbp != null ? Number(p.pnl_gbp) : null,
      };
    });
}

function mapStatePositions(raw: unknown): GpuExecPosition[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((p) => {
      const row = p as Record<string, unknown>;
      const dealId = String(row.deal_id || row.dealId || row.id || "");
      if (!dealId) return null;
      const epic = String(row.epic || "");
      const soft =
        row.soft_loss_gbp != null
          ? Number(row.soft_loss_gbp)
          : (row.protection_summary as { soft_loss_gbp?: number } | undefined)
              ?.soft_loss_gbp ?? null;
      const trail =
        row.trail_floor_gbp != null
          ? Number(row.trail_floor_gbp)
          : (row.protection_summary as { trail_floor_gbp?: number } | undefined)
              ?.trail_floor_gbp ?? null;
      const pnl =
        row.pnl_gbp != null
          ? Number(row.pnl_gbp)
          : row.unrealized_gbp != null
            ? Number(row.unrealized_gbp)
            : row.upl != null
              ? Number(row.upl)
              : null;
      return {
        dealId,
        epic,
        label: labelFor(epic),
        direction: String(row.direction || row.dir || "BUY").toUpperCase(),
        size: Number(row.size ?? row.dealSize ?? 0.5) || 0.5,
        entry: Number(row.entry ?? row.level ?? row.openLevel ?? 0) || 0,
        softLossGbp: soft != null && Number.isFinite(soft) ? soft : null,
        trailFloorGbp: trail != null && Number.isFinite(trail) ? trail : null,
        pnlGbp: pnl != null && Number.isFinite(pnl) ? pnl : null,
      } satisfies GpuExecPosition;
    })
    .filter((p): p is GpuExecPosition => p != null);
}

function extractGateVerdict(msg: Record<string, unknown>): string {
  const direct = msg.gate_verdict ?? msg.active_gate_verdict ?? msg.gating_reason;
  if (typeof direct === "string" && direct.trim()) return direct.trim().toUpperCase();

  const gd = msg.gate_diagnostics as
    | {
        last?: { gating_reason?: string; wait_reason?: string };
        by_epic?: Record<
          string,
          {
            gating_reason?: string;
            wait_reason?: string;
            all_passed?: boolean;
            gates?: Array<{ name?: string; passed?: boolean; why_failed?: string }>;
          }
        >;
      }
    | undefined;

  const last = gd?.last;
  if (last?.gating_reason) return String(last.gating_reason).toUpperCase();
  if (last?.wait_reason) return String(last.wait_reason).toUpperCase();

  const by = gd?.by_epic;
  if (by && typeof by === "object") {
    const preferred =
      by[DOW] ||
      by[Object.keys(by).find((k) => k.includes("DOW")) || ""] ||
      by[Object.keys(by)[0] || ""];
    if (preferred) {
      if (preferred.all_passed) {
        const passed = [...(preferred.gates || [])]
          .reverse()
          .find((g) => g.passed && g.name);
        if (passed?.name) {
          const name = String(passed.name).toUpperCase();
          return name.includes("PASSED") ? name : `${name}_PASSED`;
        }
        return "GATES_ALL_PASSED";
      }
      if (preferred.gating_reason) {
        return String(preferred.gating_reason).toUpperCase();
      }
      if (preferred.wait_reason) {
        return String(preferred.wait_reason).toUpperCase();
      }
      const failed = [...(preferred.gates || [])]
        .reverse()
        .find((g) => g.passed === false && g.name);
      if (failed?.name) {
        const why = String(failed.why_failed || "VETO").toUpperCase().replace(/\s+/g, "_");
        return `${String(failed.name).toUpperCase()}_${why}`.slice(0, 64);
      }
    }
  }

  const signal = msg.signal as { block_reason?: string } | undefined;
  if (signal?.block_reason) return String(signal.block_reason).toUpperCase();

  const block = msg.block_reason;
  if (typeof block === "string" && block.trim()) return block.trim().toUpperCase();

  const suppress = msg.last_gate_suppression_reason;
  if (typeof suppress === "string" && suppress.trim()) {
    return String(suppress).toUpperCase();
  }

  return "GATE_UNKNOWN";
}

function deriveSniperArm(msg: Record<string, unknown>, gateVerdict: string): SniperArmState {
  const explicit = msg.sniper_arm ?? msg.sniper_arming_state ?? msg.arms;
  if (typeof explicit === "string") {
    const u = explicit.toUpperCase();
    if (u.includes("ACTIVE") || u === "ARMED") return "ACTIVE";
    if (u.includes("VETO")) return "VETOED";
    if (u.includes("SUPPRESS")) return "SUPPRESSED";
  }
  if (explicit && typeof explicit === "object") {
    const sniper = String(
      (explicit as { sniper?: string; state?: string }).sniper ||
        (explicit as { state?: string }).state ||
        "",
    ).toUpperCase();
    if (sniper.includes("ACTIVE") || sniper === "ARMED") return "ACTIVE";
    if (sniper.includes("VETO")) return "VETOED";
    if (sniper) return "SUPPRESSED";
  }

  const paused = Boolean(msg.trading_paused);
  const allReady = msg.all_ready === true;
  const allReadyFalse = msg.all_ready === false;
  const qf = msg.quote_freshness as { fresh?: boolean; fail_closed?: boolean } | undefined;
  const transport = resolveFeedTransport(msg);
  const budgetMs = resolveQuoteBudgetMs(transport);
  const staleQuote =
    qf?.fresh === false ||
    Boolean(qf?.fail_closed) ||
    !quoteWithinBudget(quoteAgeMsFrom(msg), budgetMs);
  const verdict = gateVerdict.toUpperCase();
  const pathDown =
    msg.trading_path_live === false ||
    Boolean(msg.rest_pressure) ||
    Boolean(msg.entries_blocked);
  const pathBadge = String(msg.trading_path_badge || "").toUpperCase();
  const restCritical =
    pathBadge.includes("REST PRESSURE") || pathBadge.includes("DESK TRADING DOWN");

  if (
    verdict.includes("VETO") ||
    verdict.includes("FAULT") ||
    verdict.includes("KILL")
  ) {
    return "VETOED";
  }
  if (pathDown || restCritical || paused || allReadyFalse || staleQuote) {
    return "SUPPRESSED";
  }
  if (allReady && !paused && !staleQuote && !pathDown) return "ACTIVE";
  if (msg.quotes_fresh === true && !paused && !pathDown) return "ACTIVE";
  return "SUPPRESSED";
}

function quoteAgeMsFrom(msg: Record<string, unknown>): number | null {
  const qf = msg.quote_freshness as { age_sec?: number; age_ms?: number } | undefined;
  if (qf?.age_ms != null && Number.isFinite(Number(qf.age_ms))) {
    return Math.max(0, Number(qf.age_ms));
  }
  if (qf?.age_sec != null && Number.isFinite(Number(qf.age_sec))) {
    return Math.max(0, Number(qf.age_sec) * 1000);
  }
  if (msg.tick_age_s != null && Number.isFinite(Number(msg.tick_age_s))) {
    return Math.max(0, Number(msg.tick_age_s) * 1000);
  }
  if (msg.quote_age_ms != null && Number.isFinite(Number(msg.quote_age_ms))) {
    return Math.max(0, Number(msg.quote_age_ms));
  }
  if (msg.quote_age_sec != null && Number.isFinite(Number(msg.quote_age_sec))) {
    return Math.max(0, Number(msg.quote_age_sec) * 1000);
  }
  return null;
}

/** Index / commodity mids must not be poisoned by /state micro-channel (~100). */
function plausibleMidForEpic(epic: string, mid: number): boolean {
  if (!(mid > 0) || !Number.isFinite(mid)) return false;
  const e = epic.toUpperCase();
  if (e.includes("DOW") || e.includes("DAX") || e.includes("NIKKEI") || e.includes("FTSE")) {
    return mid >= 1000;
  }
  if (e.includes("GOLD") || e.includes("XAU")) return mid >= 500;
  if (e.includes("CRUDE") || e.includes("BRENT") || e.includes("OIL")) {
    return mid >= 20 && mid < 500;
  }
  if (e.includes("EURUSD") || e.includes("FX")) return mid > 0.5 && mid < 2.5;
  // Reject generic micro-channel band when epic defaults to DOW
  if (e.includes("IFM") || e.includes("IX.")) return mid >= 1000;
  return mid < 500 ? false : true;
}

function extractTicks(msg: Record<string, unknown>): DeskTickChannel[] {
  const out: DeskTickChannel[] = [];
  const ageMs = quoteAgeMsFrom(msg);

  const push = (
    epic: string,
    mid: number,
    bid: number,
    offer: number,
    tickAgeMs: number | null,
  ) => {
    if (!plausibleMidForEpic(epic, mid)) return;
    out.push({ epic, mid, bid, offer, ageMs: tickAgeMs });
  };

  if (Array.isArray(msg.ticks)) {
    for (const t of msg.ticks) {
      const row = t as Record<string, unknown>;
      const mid = Number(row.mid ?? row.last ?? 0);
      const epic = String(row.epic || DOW);
      push(epic, mid, Number(row.bid ?? 0), Number(row.offer ?? 0), row.age_ms != null ? Number(row.age_ms) : ageMs);
    }
  }

  // Top-level /state bid~100 is micro-channel — never bind as DOW mid
  const topMid = Number(msg.mid ?? msg.last_mid ?? 0);
  const topBid = Number(msg.bid ?? 0);
  const topOffer = Number(msg.offer ?? 0);
  const topEpic = String(msg.epic || "");
  if (topEpic && (topMid > 0 || (topBid > 0 && topOffer > topBid))) {
    const resolved = topMid > 0 ? topMid : (topBid + topOffer) / 2;
    push(topEpic, resolved, topBid, topOffer, ageMs);
  }

  const mq = msg.market_quotes as
    | Record<string, { mid?: number; bid?: number; offer?: number; last_price?: number }>
    | undefined;
  if (mq && typeof mq === "object") {
    for (const [epic, q] of Object.entries(mq)) {
      const mid =
        Number(q?.mid ?? q?.last_price ?? 0) ||
        (Number(q?.bid ?? 0) > 0 && Number(q?.offer ?? 0) > Number(q?.bid ?? 0)
          ? (Number(q.bid) + Number(q.offer)) / 2
          : 0);
      push(epic, mid, Number(q?.bid ?? 0), Number(q?.offer ?? 0), ageMs);
    }
  }

  // market_quotes last so they win dedupe over weaker sources
  const map = new Map<string, DeskTickChannel>();
  for (const t of out) map.set(t.epic, t);
  return [...map.values()];
}

/** Normalize any agent frame (native mux or /state|/ws/stream|/fulfillment) into desk envelope. */
export function normalizeDeskMultiplex(
  raw: unknown,
  source: DeskMultiplexEnvelope["source"] = "ws",
): DeskMultiplexEnvelope {
  const msg = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;

  // Native multiplex envelope from microkernel
  if (
    msg.mux === true ||
    msg.channel === "desk_mux" ||
    (msg.ticks != null && msg.positions != null && msg.arms != null)
  ) {
    const ticks = extractTicks(msg);
    const gateVerdict = extractGateVerdict(msg);
    const armsRaw = (msg.arms || {}) as Record<string, unknown>;
    const sniper = deriveSniperArm(
      { ...msg, sniper_arm: armsRaw.sniper ?? msg.sniper_arm },
      gateVerdict,
    );
    const session = (msg.session_pnl || msg.sessionPnl || {}) as Record<string, unknown>;
    const ageMs = quoteAgeMsFrom(msg);
    const transport = resolveFeedTransport(msg);
    const positionsOut = Array.isArray(msg.positions)
      ? mapStatePositions(msg.positions).length
        ? mapStatePositions(msg.positions)
        : mapLivePositions(msg.positions as LivePositionRow[])
      : [];
    const engines = buildEngineLanes(msg, positionsOut, ageMs, transport);
    return {
      mux: true,
      ticks,
      positions: positionsOut,
      arms: {
        sniper,
        allReady:
          msg.all_ready != null
            ? Boolean(msg.all_ready)
            : armsRaw.all_ready != null
              ? Boolean(armsRaw.all_ready)
              : null,
        tradingPaused:
          msg.trading_paused != null
            ? Boolean(msg.trading_paused)
            : armsRaw.trading_paused != null
              ? Boolean(armsRaw.trading_paused)
              : null,
        reason: String(armsRaw.reason || gateVerdict || ""),
      },
      session_pnl: {
        realizedGbp: Number(
          session.realized_gbp ?? session.realizedGbp ?? msg.realized_daily_pnl_gbp ?? 0,
        ),
        unrealizedGbp: Number(
          session.unrealized_gbp ??
            session.unrealizedGbp ??
            msg.open_unrealized_gbp ??
            0,
        ),
        dailyGbp: Number(session.daily_gbp ?? session.dailyGbp ?? msg.daily_pnl_gbp ?? 0),
      },
      truth: {
        quoteAgeMs: ageMs,
        sniperArm: sniper,
        gateVerdict,
        updatedAt: performance.now(),
      },
      source,
      engines,
      feedTransport: transport,
    };
  }

  const ticks = extractTicks(msg);
  const positionsOut = mapStatePositions(msg.positions).length
    ? mapStatePositions(msg.positions)
    : mapLivePositions(
        ((msg.positions as LivePositionRow[]) ||
          (msg.active_positions as LivePositionRow[]) ||
          []) as LivePositionRow[],
      );
  const gateVerdict = extractGateVerdict(msg);
  const sniper = deriveSniperArm(msg, gateVerdict);
  const ageMs = quoteAgeMsFrom(msg);
  const transport = resolveFeedTransport(msg);
  const engines = buildEngineLanes(msg, positionsOut, ageMs, transport);

  return {
    mux: true,
    ticks,
    positions: positionsOut,
    arms: {
      sniper,
      allReady: msg.all_ready != null ? Boolean(msg.all_ready) : null,
      tradingPaused: msg.trading_paused != null ? Boolean(msg.trading_paused) : null,
      reason: gateVerdict,
    },
    session_pnl: {
      realizedGbp: Number(msg.realized_daily_pnl_gbp ?? 0),
      unrealizedGbp: Number(msg.open_unrealized_gbp ?? 0),
      dailyGbp: Number(msg.daily_pnl_gbp ?? 0),
    },
    truth: {
      quoteAgeMs: ageMs,
      sniperArm: sniper,
      gateVerdict,
      updatedAt: performance.now(),
    },
    source,
    engines,
    feedTransport: transport,
  };
}
