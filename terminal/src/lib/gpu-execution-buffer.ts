/**
 * Mutable RAM buffer for hybrid desk (GPU chart + ref blotter + truth strip).
 * Written by multiplex WS ingest; read by RAF / DOM refs — never React hot path.
 */

import type {
  DeskArmsChannel,
  DeskEngineLane,
  DeskExecutionTruth,
  DeskSessionPnlChannel,
  DualPortHealth,
  SniperArmState,
} from "@/lib/desk-multiplex";
import { resolveRuntimeAssetProfile } from "@/lib/runtime-asset-profiles";

export type GpuExecPosition = {
  dealId: string;
  epic: string;
  label: string;
  direction: string;
  size: number;
  entry: number;
  softLossGbp: number | null;
  trailFloorGbp: number | null;
  pnlGbp: number | null;
};

export type GpuExecutionBuffer = {
  revision: number;
  structureRevision: number;
  mids: number[];
  lastMid: number;
  focusEpic: string;
  focusLabel: string;
  positions: GpuExecPosition[];
  trailCrawl: number[];
  softCrawl: number[];
  wsLive: boolean;
  updatedAt: number;
  arms: DeskArmsChannel;
  sessionPnl: DeskSessionPnlChannel;
  truth: DeskExecutionTruth;
  lastMuxSource: "ws" | "bootstrap" | "recovery" | "idle";
  brokerOpenSotCount: number | null;
  engines: DeskEngineLane[];
  feedTransport: string;
  portHealth: DualPortHealth;
};

export const GPU_MID_CAP = 240;
export const GPU_CRAWL_CAP = 120;

export function createGpuExecutionBuffer(): GpuExecutionBuffer {
  return {
    revision: 0,
    structureRevision: 0,
    mids: [],
    lastMid: 0,
    focusEpic: "IX.D.DOW.IFM.IP",
    focusLabel: "WALL ST / DOW",
    positions: [],
    trailCrawl: [],
    softCrawl: [],
    wsLive: false,
    updatedAt: 0,
    arms: {
      sniper: "SUPPRESSED",
      allReady: null,
      tradingPaused: null,
      reason: "",
    },
    sessionPnl: { realizedGbp: 0, unrealizedGbp: 0, dailyGbp: 0 },
    truth: {
      quoteAgeMs: null,
      sniperArm: "SUPPRESSED" as SniperArmState,
      gateVerdict: "GATE_UNKNOWN",
      updatedAt: 0,
    },
    lastMuxSource: "idle",
    brokerOpenSotCount: null,
    engines: [],
    feedTransport: "rest_poll",
    portHealth: {
      cfd: { online: false, healthOk: false, quoteAgeMs: null },
      sb: { online: false, healthOk: false, quoteAgeMs: null },
    },
  };
}

export function pushMid(buf: GpuExecutionBuffer, mid: number): void {
  if (!(mid > 0)) return;
  const last = buf.mids[buf.mids.length - 1];
  if (last === mid) {
    buf.lastMid = mid;
    buf.updatedAt = performance.now();
    return;
  }
  buf.mids.push(mid);
  if (buf.mids.length > GPU_MID_CAP) {
    buf.mids.splice(0, buf.mids.length - GPU_MID_CAP);
  }
  buf.lastMid = mid;
  buf.revision += 1;
  buf.updatedAt = performance.now();
}

export function pushCrawl(ring: number[], level: number | null): void {
  if (level == null || !(level > 0)) return;
  const last = ring[ring.length - 1];
  if (last != null && Math.abs(last - level) < 1e-9) return;
  ring.push(level);
  if (ring.length > GPU_CRAWL_CAP) {
    ring.splice(0, ring.length - GPU_CRAWL_CAP);
  }
}

export function gbpToPriceLevel(
  entry: number,
  direction: string,
  gbp: number,
  size: number,
  kind: "stop" | "profit",
  epic?: string | null,
): number | null {
  if (!(entry > 0) || !(size > 0) || !Number.isFinite(gbp) || gbp <= 0) {
    return null;
  }
  // Spreadbet: size is £/pt (indices) or £/pip (FX) → raw move = gbp / size.
  const raw = Math.abs(gbp) / size;
  let priceDelta = raw;
  if (epic) {
    const prof = resolveRuntimeAssetProfile(epic);
    if (prof.isForex) {
      // raw is pips → price units
      priceDelta = raw / Math.max(prof.pointMultiplier, 1);
    }
  }
  const buy = direction.toUpperCase() !== "SELL";
  if (kind === "stop") return buy ? entry - priceDelta : entry + priceDelta;
  return buy ? entry + priceDelta : entry - priceDelta;
}

export function positionsFingerprint(positions: GpuExecPosition[]): string {
  return positions
    .map((p) => p.dealId)
    .sort()
    .join("|");
}
