"use client";

/**
 * Hybrid Quantum Node memory plane.
 * Merges async agent APIs + WS telemetry into throttled React views.
 * High-frequency tick rings live in refs — no disk polling.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  agentWsBase,
  fetchAgentJson,
  type FulfillmentPayload,
  type LivePositionRow,
  type LivePositionsPayload,
  type TelemetryPayload,
} from "@/lib/agent-client";
import {
  cfdHttpBase,
  fetchDeskJson,
  sbHttpBase,
} from "@/lib/desk-api-bases";
import { liveQuoteForEpic } from "@/lib/fulfillment-parse";
import { EPIC_LABELS } from "@/lib/constants";
import { gbpToPriceLevel } from "@/lib/gpu-execution-buffer";
import {
  plausibleMidForEpic,
  resolveRuntimeAssetProfile,
} from "@/lib/runtime-asset-profiles";
import type {
  AlphaPoint,
  AssetMarketNode,
  PositionAuthorityRow,
  QuantumNodeView,
  QuantumSafetyMatrix,
  ScannerRankedChrome,
  SniperMarketRow,
  SniperRankLane,
  SniperStatusKind,
} from "@/lib/quantum-node-types";
import { useFulfillment } from "@/hooks/useFulfillment";
import { useOpenPositions } from "@/hooks/useOpenPositions";

const TICK_CAP = 72;
const ALPHA_CAP = 96;
const UI_HZ = 5;
const B1_FLOOR = -100;
const ATR_TP_MULT = 3.5;
const OBI_CLAMP = 0.15;

const DOW = "IX.D.DOW.IFM.IP";
const DAX = "IX.D.DAX.IFM.IP";
const CRUDE = "CS.D.CRUDE.CFD.IP";
const GOLD = "CS.D.CFPGOLD.CFP.IP";
const EURUSD = "CS.D.EURUSD.CFD.IP";
const FTSE = "IX.D.FTSE.IFM.IP";
const NIKKEI = "IX.D.NIKKEI.IFM.IP";

/** Hot-path excluded until JPY PnL certified — never pretend Nikkei is promoted. */
const RANKED_EXCLUDED_EPICS = new Set([NIKKEI, DAX, CRUDE, "CS.D.GBPUSD.CFD.IP"]);

type RiskMonitorPayload = {
  ok?: boolean;
  positions?: Array<{
    deal_id?: string;
    epic?: string;
    direction?: string;
    size?: number;
    entry?: number;
    pnl_gbp?: number | null;
  }>;
  total_unrealized_gbp?: number;
};

type TradeStatePayload = {
  stops?: {
    positions?: Array<{
      deal_id?: string;
      epic?: string;
      direction?: string;
      entry_level?: number;
      size?: number;
    }>;
  };
};

type TradeSupportPayload = {
  total_unrealized_gbp?: number | null;
  broker_open?: number;
  running?: boolean;
  status_age_sec?: number | null;
};

type RegimePayload = {
  markets?: Array<{
    epic?: string;
    atr?: number;
    atr_ratio?: number;
    state_label?: string;
    strategy_gate?: { allow_entries?: boolean; mode?: string };
    healthy?: boolean;
    reason?: string;
  }>;
};

type RotationInstrument = {
  epic?: string;
  label?: string;
  ticks_per_minute?: number;
  velocity?: number;
  z_score?: number;
  spread_ok?: boolean;
  reason?: string;
};

type RankedRotatorRow = {
  epic?: string;
  eligible?: boolean;
  rank?: number;
  score?: number;
};

type RankedRotator = {
  active?: boolean;
  mode?: string;
  dominant?: string | null;
  promoted?: string[];
  reason?: string;
  rows?: RankedRotatorRow[];
};

type RotationPayload = {
  ok?: boolean;
  rotation?: {
    active_instruments?: RotationInstrument[];
    eligible_instruments?: RotationInstrument[];
    inactive_instruments?: RotationInstrument[];
    stagnant_dead_zone_epics?: Record<string, number>;
    pinned_open_epics?: string[];
    last_rotation_reason?: string;
    ranked_rotator?: RankedRotator | null;
  };
};

type HealthPauseSlice = {
  trading_paused?: boolean;
  agent_alive?: boolean;
};

function shortRankLabel(epic: string | null | undefined): string {
  const e = String(epic || "").trim();
  if (!e) return "";
  if (e === DOW || e.includes("DOW")) return "DOW";
  if (e === GOLD || e.includes("CFPGOLD") || e.includes("GOLD")) return "GOLD";
  if (e.includes("EURUSD")) return "EUR";
  if (e.includes("FTSE")) return "FTSE";
  if (e.includes("NIKKEI")) return "NIKKEI";
  if (e.includes("DAX")) return "DAX";
  if (e.includes("CRUDE")) return "CRUDE";
  return e.split(".")[2] || e.slice(0, 8);
}

function pickPreferredRotation(
  cfd: RotationPayload | null,
  sb: RotationPayload | null,
  preferSb: boolean,
): RotationPayload | null {
  const order = preferSb ? [sb, cfd] : [cfd, sb];
  for (const slice of order) {
    if (slice?.rotation?.ranked_rotator?.active) return slice;
  }
  for (const slice of order) {
    if (slice?.rotation) return slice;
  }
  return cfd || sb;
}

function buildRankedChrome(rotation: RotationPayload | null): ScannerRankedChrome {
  const rr = rotation?.rotation?.ranked_rotator;
  if (!rr?.active) {
    return {
      active: false,
      dominant: null,
      promotedLabels: [],
      waitingLabels: [],
      excludedNote: null,
    };
  }
  const promoted = new Set(
    (rr.promoted || []).map((e) => String(e).trim()).filter(Boolean),
  );
  const waiting: string[] = [];
  for (const row of [...(rr.rows || [])].sort(
    (a, b) => Number(a.rank ?? 99) - Number(b.rank ?? 99),
  )) {
    const epic = String(row.epic || "").trim();
    if (!epic || promoted.has(epic)) continue;
    const label = shortRankLabel(epic);
    if (label && !waiting.includes(label)) waiting.push(label);
  }
  return {
    active: true,
    dominant: shortRankLabel(rr.dominant) || null,
    promotedLabels: (rr.promoted || [])
      .map((e) => shortRankLabel(e))
      .filter(Boolean),
    waitingLabels: waiting,
    excludedNote: "NIKKEI excl",
  };
}

type OpsStrip = {
  grok_macro_bias?: string;
  atr_reward_risk?: number;
  desk_idle_reason?: { code?: string; label?: string } | null;
  trading_path_live?: boolean;
  trading_path_badge?: string;
  rest_pressure?: boolean;
  rest_pressure_level?: string;
  sniper_ml?: {
    p_success?: number | null;
    approved?: boolean;
    threshold?: number;
    epic?: string;
  };
};

type SniperMlEpicRow = {
  p_success?: number;
  approved?: boolean;
  threshold?: number;
  reason?: string;
};

type SniperMlPayload = {
  ok?: boolean;
  threshold?: number;
  by_epic?: Record<string, SniperMlEpicRow>;
  latest?: SniperMlEpicRow & { epic?: string; p_success?: number | null };
};

type Enrichment = {
  risk: RiskMonitorPayload | null;
  tradeState: TradeStatePayload | null;
  tradeSupport: TradeSupportPayload | null;
  regime: RegimePayload | null;
  ops: OpsStrip | null;
  rotation: RotationPayload | null;
  sniperMl: SniperMlPayload | null;
};

function midOf(payload: FulfillmentPayload | null, epic: string): number {
  const q = liveQuoteForEpic(payload, epic);
  if (!q) return 0;
  const candidates = [
    q.mid,
    q.last_price,
    q.bid > 0 && q.offer > 0 ? (q.bid + q.offer) / 2 : 0,
    q.bid,
    q.offer,
  ];
  for (const m of candidates) {
    if (plausibleMidForEpic(epic, Number(m))) return Number(m);
  }
  return 0;
}

function pushTick(map: Map<string, number[]>, epic: string, mid: number) {
  if (!(mid > 0)) return;
  const arr = map.get(epic) ?? [];
  const last = arr[arr.length - 1];
  if (last === mid && arr.length > 0) return;
  arr.push(mid);
  if (arr.length > TICK_CAP) arr.splice(0, arr.length - TICK_CAP);
  map.set(epic, arr);
}

function clampObi(v: number): number {
  return Math.max(-OBI_CLAMP, Math.min(OBI_CLAMP, v));
}

/** Hub-proxy OBI from quote micro-structure + mid drift (±0.15 band). */
function estimateObi(
  bid: number,
  offer: number,
  mid: number,
  prevMid: number,
  atr: number,
): number {
  if (!(bid > 0) || !(offer > bid) || !(mid > 0)) return 0;
  const spread = offer - bid;
  const skew = ((mid - bid) - (offer - mid)) / Math.max(spread, 1e-9);
  const drift =
    prevMid > 0 && atr > 0 ? (mid - prevMid) / Math.max(atr * 0.15, 1e-6) : 0;
  return clampObi(skew * 0.08 + drift * 0.05);
}

function atrFor(regime: RegimePayload | null, epic: string): number {
  const row = regime?.markets?.find((m) => m.epic === epic);
  const atr = Number(row?.atr ?? 0);
  return atr > 0 ? atr : epic === DOW ? 14.14 : 1.7;
}

function fmtGbp(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}£${Math.abs(v) < 1000 ? v.toFixed(2) : v.toFixed(0)}`;
}

function fmtPx(v: number, epic: string): string {
  if (!(v > 0)) return "—";
  if (epic.includes("EURUSD")) return v.toFixed(5);
  if (epic.includes("CRUDE") || epic.includes("GOLD")) return v.toFixed(2);
  return v.toFixed(1);
}

function mergePositions(
  live: LivePositionsPayload | null,
  enrich: Enrichment,
  midByEpic: Map<string, number>,
): PositionAuthorityRow[] {
  const byDeal = new Map<string, LivePositionRow>();
  for (const p of live?.positions ?? []) {
    if (p?.deal_id) byDeal.set(p.deal_id, p);
  }
  for (const p of enrich.risk?.positions ?? []) {
    const id = String(p.deal_id || "");
    if (!id) continue;
    const prev = byDeal.get(id);
    byDeal.set(id, {
      deal_id: id,
      epic: String(p.epic || prev?.epic || ""),
      direction: String(p.direction || prev?.direction || "—"),
      size: Number(p.size ?? prev?.size ?? 0),
      entry: Number(p.entry ?? prev?.entry ?? 0) || Number(prev?.entry ?? 0),
      pnl_gbp:
        p.pnl_gbp != null
          ? Number(p.pnl_gbp)
          : prev?.pnl_gbp != null
            ? Number(prev.pnl_gbp)
            : null,
      gbp_armed: prev?.gbp_armed ?? true,
      virtual_armed: prev?.virtual_armed ?? true,
      dynamic_armed: prev?.dynamic_armed ?? true,
      loss_cap_gbp: prev?.loss_cap_gbp,
      soft_loss_gbp: prev?.soft_loss_gbp,
      target_gbp: prev?.target_gbp,
      protection_summary: prev?.protection_summary,
    });
  }
  for (const s of enrich.tradeState?.stops?.positions ?? []) {
    const id = String(s.deal_id || "");
    if (!id) continue;
    const prev = byDeal.get(id);
    const entry = Number(s.entry_level ?? 0);
    if (!prev) {
      byDeal.set(id, {
        deal_id: id,
        epic: String(s.epic || ""),
        direction: String(s.direction || "BUY"),
        size: Number(s.size ?? 0.5),
        entry,
        pnl_gbp: null,
        gbp_armed: true,
        virtual_armed: true,
        dynamic_armed: true,
      });
    } else if (!(prev.entry > 0) && entry > 0) {
      byDeal.set(id, { ...prev, entry });
    }
  }

  const supportUpl = enrich.tradeSupport?.total_unrealized_gbp;
  const rows: PositionAuthorityRow[] = [];
  for (const p of byDeal.values()) {
    const atr = atrFor(enrich.regime, p.epic);
    const entry = p.entry > 0 ? p.entry : 0;
    const dir = (p.direction || "BUY").toUpperCase();
    const tp =
      entry > 0
        ? dir === "SELL"
          ? entry - ATR_TP_MULT * atr
          : entry + ATR_TP_MULT * atr
        : null;
    let pnl = p.pnl_gbp;
    if (pnl == null && supportUpl != null && byDeal.size === 1) {
      pnl = Number(supportUpl);
    }
    const b1: PositionAuthorityRow["b1Status"] =
      pnl == null ? "UNKNOWN" : pnl <= B1_FLOOR ? "BREACHED" : "OK";
    const g = p.protection_summary?.gbp_armed ?? p.gbp_armed;
    const v = p.protection_summary?.virtual_armed ?? p.virtual_armed;
    const d = p.protection_summary?.dynamic_armed ?? p.dynamic_armed;
    const soft =
      p.protection_summary?.soft_loss_gbp ?? p.soft_loss_gbp ?? null;
    const trail =
      p.protection_summary?.trail_floor_gbp ?? p.trail_floor_gbp ?? null;
    const target = p.protection_summary?.target_gbp ?? p.target_gbp ?? null;
    const peak =
      p.protection_summary?.peak_profit_gbp ?? p.peak_profit_gbp ?? null;
    const mid = midByEpic.get(p.epic) ?? midByEpic.get(DOW) ?? 0;
    rows.push({
      dealId: p.deal_id,
      epic: p.epic,
      label: EPIC_LABELS[p.epic] || p.epic.split(".")[2] || p.epic,
      direction: dir,
      size: p.size,
      entry,
      currentMid: mid > 0 ? mid : null,
      pnlGbp: pnl,
      peakProfitGbp: peak != null ? Number(peak) : null,
      atr,
      atrMultiple: ATR_TP_MULT,
      takeProfit: tp,
      softLossGbp: soft != null ? Number(soft) : null,
      trailFloorGbp: trail != null ? Number(trail) : null,
      targetGbp: target != null ? Number(target) : null,
      b1FloorGbp: B1_FLOOR,
      b1Status: b1,
      layers: `${g ? "G" : "—"}${v ? "V" : "—"}${d ? "D" : "—"}`,
      bias: `${dir} ${p.size}`,
    });
  }
  return rows;
}

function findRotationRow(
  rotation: RotationPayload | null,
  epic: string,
): RotationInstrument | null {
  const rot = rotation?.rotation;
  if (!rot) return null;
  const all = [
    ...(rot.active_instruments ?? []),
    ...(rot.eligible_instruments ?? []),
  ];
  return all.find((r) => r.epic === epic) ?? null;
}

const SNIPER_GATE = 0.68;

/** Match src/alpha/micro_sniper_ml.py asset-class adaptive thresholds. */
function sniperThresholdForEpic(epic: string, apiThr?: number | null): number {
  if (apiThr != null && Number.isFinite(Number(apiThr))) {
    return Number(apiThr);
  }
  const key = String(epic || "").toUpperCase();
  if (key.includes("GOLD") || key.includes("XAU") || key.includes("CFPGOLD")) {
    return 0.74;
  }
  if (
    key.includes("EURUSD") ||
    key.includes("GBPUSD") ||
    key.includes("USDJPY") ||
    (key.startsWith("CS.D.") && key.includes("CFD.IP") && !key.includes("CRUDE"))
  ) {
    return 0.7;
  }
  return SNIPER_GATE;
}
/** EMA alpha for AI Matrix Regime Score — dampens poll-to-poll flicker */
const CONVICTION_EMA_ALPHA = 0.35;
/** Hold prior display unless move exceeds this (absolute P units) for 2 samples */
const CONVICTION_HOLD_EPS = 0.025;
/** Status text must agree for N compose ticks before swapping (stops 2.5–5s thrash) */
const STATUS_HOLD_SAMPLES = 3;

function shortEpicTag(epic: string): string {
  if (epic === DOW) return "DOW";
  if (epic === FTSE) return "UK100_FTSE";
  if (epic === GOLD) return "GOLD";
  if (epic === EURUSD) return "FX_EURUSD";
  if (epic === DAX) return "DAX";
  if (epic === CRUDE) return "CRUDE";
  const parts = epic.split(".");
  return parts[2] || epic.slice(0, 12);
}

function buildScanner(
  fulfillment: FulfillmentPayload | null,
  ticks: Map<string, number[]>,
  positions: PositionAuthorityRow[],
  enrich: Enrichment,
  convictionEma: Map<string, number>,
  convictionHold: Map<string, { p: number; streak: number }>,
  statusHold: Map<
    string,
    { text: string; kind: SniperStatusKind; streak: number }
  >,
): SniperMarketRow[] {
  const bias = (enrich.ops?.grok_macro_bias || "NEUTRAL").toUpperCase();
  const stagnant = enrich.rotation?.rotation?.stagnant_dead_zone_epics ?? {};
  const activeList = enrich.rotation?.rotation?.active_instruments ?? [];
  const eligibleSet = new Set(
    (enrich.rotation?.rotation?.eligible_instruments ?? []).map((r) =>
      String(r.epic || ""),
    ),
  );
  const activeSet = new Set(activeList.map((r) => String(r.epic || "")));
  const ranked = enrich.rotation?.rotation?.ranked_rotator;
  const rankedOn = ranked?.active === true;
  const promotedSet = new Set(
    (ranked?.promoted || []).map((e) => String(e).trim()).filter(Boolean),
  );
  const rankedRowByEpic = new Map<string, RankedRotatorRow>();
  for (const row of ranked?.rows || []) {
    const epic = String(row.epic || "").trim();
    if (epic) rankedRowByEpic.set(epic, row);
  }
  // Hot-path proxy = ranked dominant when active, else primary stack epic
  const hotProxyEpic = rankedOn && ranked?.dominant
    ? String(ranked.dominant)
    : activeList[0]?.epic
      ? String(activeList[0].epic)
      : DOW;
  const hotProxyTag = shortEpicTag(hotProxyEpic);

  const targets: Array<{
    id: SniperMarketRow["id"];
    label: string;
    epic: string;
  }> = [
    { id: "dow", label: "DOW / WALL ST", epic: DOW },
    { id: "ftse", label: "FTSE 100", epic: FTSE },
    { id: "gold", label: "GOLD", epic: GOLD },
    { id: "eurusd", label: "EUR/USD", epic: EURUSD },
  ];

  return targets.map((t) => {
    const assetProf = resolveRuntimeAssetProfile(t.epic);
    const mid = midOf(fulfillment, t.epic);
    const rot = findRotationRow(enrich.rotation, t.epic);
    const regime = enrich.regime?.markets?.find((m) => m.epic === t.epic);
    const allowEntries = Boolean(regime?.strategy_gate?.allow_entries);
    const regimeLabel = String(regime?.state_label || "unknown").toUpperCase();
    const inActiveStack = activeSet.has(t.epic);
    const inEligible = eligibleSet.has(t.epic);
    const rankedRow = rankedRowByEpic.get(t.epic);
    const inPromoted = rankedOn && promotedSet.has(t.epic);
    const inRankedCandidate = rankedOn && rankedRow != null;
    const rankedEligible = Boolean(rankedRow?.eligible);
    const excludedHot = RANKED_EXCLUDED_EPICS.has(t.epic);
    let rankLane: SniperRankLane = null;
    if (excludedHot) rankLane = "excluded";
    else if (inPromoted) rankLane = "promoted";
    else if (inRankedCandidate && rankedEligible) rankLane = "eligible";
    else if (inRankedCandidate) rankLane = "waiting";
    else if (inActiveStack) rankLane = "stack";
    // Ranked promote is the SB hot allowlist — do not require dual-core stack slot
    const hotPathMember = rankedOn ? inPromoted : inActiveStack;
    const velocity = Number(rot?.velocity ?? 0);
    const zScore = Number(rot?.z_score ?? 0);
    const tpm = Number(rot?.ticks_per_minute ?? 0);
    const openOn = positions.filter((p) => p.epic === t.epic);
    const focusOpen = openOn[0] ?? null;
    const openBuy = openOn.some((p) => p.direction === "BUY");
    const openSell = openOn.some((p) => p.direction === "SELL");
    const trailFloorGbp = focusOpen?.trailFloorGbp ?? null;
    const softLossGbp = focusOpen?.softLossGbp ?? null;
    const trailPriceLevel =
      focusOpen && trailFloorGbp != null && trailFloorGbp > 0
        ? gbpToPriceLevel(
            focusOpen.entry,
            focusOpen.direction,
            trailFloorGbp,
            focusOpen.size,
            "profit",
            focusOpen.epic,
          )
        : null;
    const regimeReason = String(regime?.reason || "");
    const insufficientBars = regimeReason.toLowerCase().includes("insufficient");
    const chop =
      !insufficientBars &&
      (regimeLabel === "CHOP" || regimeLabel === "RANGE");
    const volVeto =
      Number(regime?.atr_ratio ?? 1) >= 2.2 ||
      String(regime?.strategy_gate?.mode || "") === "halt";
    const deadZone = t.epic in stagnant;
    // Live QuantumSniperMLCore P(Success) when API present; else velocity proxy
    const mlRow = enrich.sniperMl?.by_epic?.[t.epic];
    const mlPRaw =
      mlRow?.p_success != null && Number.isFinite(Number(mlRow.p_success))
        ? Math.max(0, Math.min(1, Number(mlRow.p_success)))
        : null;
    const epicGate = sniperThresholdForEpic(
      t.epic,
      mlRow?.threshold ?? enrich.sniperMl?.threshold,
    );
    const mlApproved =
      Boolean(mlRow?.approved) || (mlPRaw != null && mlPRaw >= epicGate);
    const rawConviction =
      mlPRaw != null
        ? mlPRaw
        : Math.max(
            0,
            Math.min(
              1,
              (hotPathMember ? 0.35 : 0.1) +
                Math.min(0.35, Math.abs(zScore) * 0.4) +
                Math.min(0.3, velocity / 200),
            ),
          );
    // Per-epic EMA + 2-sample hold — prevents cross-epic contamination / flicker
    const prevEma = convictionEma.get(t.epic);
    const ema =
      prevEma == null
        ? rawConviction
        : prevEma + CONVICTION_EMA_ALPHA * (rawConviction - prevEma);
    convictionEma.set(t.epic, ema);
    const hold = convictionHold.get(t.epic) ?? { p: ema, streak: 0 };
    let conviction = hold.p;
    if (Math.abs(ema - hold.p) >= CONVICTION_HOLD_EPS) {
      const streak = hold.streak + 1;
      if (streak >= 2) {
        conviction = ema;
        convictionHold.set(t.epic, { p: ema, streak: 0 });
      } else {
        convictionHold.set(t.epic, { p: hold.p, streak });
      }
    } else {
      convictionHold.set(t.epic, { p: hold.p, streak: 0 });
    }

    let statusKind: SniperStatusKind = "proxy";
    let statusText = `STATUS: ASSET IDLE · SCANNING`;
    let profile = rot?.reason || regime?.strategy_gate?.mode || "SCANNING";

    const quotesFresh = Boolean(
      fulfillment?.quote_freshness?.fresh ?? fulfillment?.all_ready,
    );
    const pathLive = enrich.ops?.trading_path_live === true;
    const restHot = enrich.ops?.rest_pressure === true;
    const sniperArmed =
      pathLive &&
      !restHot &&
      !chop &&
      !volVeto &&
      !deadZone &&
      allowEntries &&
      hotPathMember &&
      !excludedHot &&
      quotesFresh &&
      Boolean(fulfillment?.all_ready) &&
      !Boolean(fulfillment?.trading_paused);

    const deskIdleCode = String(enrich.ops?.desk_idle_reason?.code || "");
    const why = !pathLive
      ? "PATH_DOWN"
      : restHot
        ? `REST_${String(enrich.ops?.rest_pressure_level || "HOT").toUpperCase()}`
        : volVeto
          ? "VOL_VETO"
          : insufficientBars
            ? "INSUFFICIENT_BARS"
            : deskIdleCode === "us_close"
              ? "US_CLOSE"
              : chop
                ? "RANGE_BOUND"
                : deadZone
                  ? "STAGNANT_DZ"
                  : excludedHot
                    ? "EXCLUDED_HOT_PATH"
                    : !allowEntries
                      ? "ENTRIES_GATED"
                      : !hotPathMember
                        ? rankedOn
                          ? inRankedCandidate
                            ? rankedEligible
                              ? "RANKED_WAITING"
                              : "RANKED_CANDIDATE"
                            : "RANKED_IDLE"
                          : "ROTATION_IDLE"
                        : !quotesFresh
                          ? "QUOTE_STALE"
                          : "WAITING_BIAS";

    if (openBuy) {
      statusKind = "long";
      // Open book ≠ sniper arm — do not claim ENGAGED on stale/paused feed
      statusText = sniperArmed
        ? "LONG OPEN · SNIPER ARMED"
        : "LONG OPEN · MANAGING (ENTRIES SUPPRESSED)";
      profile = "OPEN_LONG · SOFTWARE STOPS";
    } else if (openSell) {
      statusKind = "short";
      statusText = sniperArmed
        ? "SHORT OPEN · BOUNDARY ARMED"
        : "SHORT OPEN · MANAGING (ENTRIES SUPPRESSED)";
      profile = "OPEN_SHORT · SOFTWARE STOPS";
    } else if (
      sniperArmed &&
      (bias === "BULLISH" || bias === "LONG" || zScore > 0.15 || velocity > 40)
    ) {
      statusKind = "long";
      statusText = "LONG SNIPER ENGAGED";
      profile = `STACK · z=${zScore.toFixed(2)} · ${tpm}tpm`;
    } else if (
      sniperArmed &&
      (bias === "BEARISH" || bias === "SHORT" || zScore < -0.15)
    ) {
      statusKind = "short";
      statusText = "SHORT BOUNDARY ARMED";
      profile = `BOUNDARY · z=${zScore.toFixed(2)} · ${tpm}tpm`;
    } else if (excludedHot) {
      statusKind = "proxy";
      statusText = "STATUS: EXCLUDED · HOT PATH";
      profile = `EXCLUDED · bias ${bias}`;
    } else if (mlApproved && !sniperArmed) {
      // ML edge ≠ strategy armed — keep statusKind proxy, wording consistent
      statusKind = "proxy";
      statusText = `STATUS: ML EDGE · GATES ${why}`;
      profile = `${why} · bias ${bias}`;
    } else if (inPromoted) {
      statusKind = "proxy";
      statusText = `STATUS: RANKED PROMOTED · ${why}`;
      profile = `PROMOTED · bias ${bias}`;
    } else if (inRankedCandidate && rankedEligible) {
      statusKind = "proxy";
      statusText = `STATUS: RANKED ELIGIBLE · ${why}`;
      profile = `ELIGIBLE · bias ${bias}`;
    } else if (inRankedCandidate) {
      statusKind = "proxy";
      statusText = `STATUS: RANKED WAITING · ${why}`;
      profile = `WAITING · bias ${bias}`;
    } else if (inActiveStack) {
      statusKind = "proxy";
      statusText = `STATUS: HOT PATH · ${why}`;
      profile = `${why} · bias ${bias}`;
    } else if (inEligible) {
      statusKind = "proxy";
      statusText = `STATUS: ROTATION ELIGIBLE · ${why}`;
      profile = `${why} · bias ${bias}`;
    } else {
      statusKind = "proxy";
      // Only non-hot assets advertise scanning the live hot-path proxy
      statusText =
        t.epic === hotProxyEpic
          ? `STATUS: ASSET IDLE · ${why}`
          : `STATUS: ASSET IDLE · PROXY [${hotProxyTag}] · ${why}`;
      profile = `${why} · bias ${bias}`;
    }

    // Hold status text/kind across compose ticks so enrichment jitter doesn't thrash
    const prevStatus = statusHold.get(t.epic);
    if (prevStatus && prevStatus.text === statusText) {
      statusHold.set(t.epic, { text: statusText, kind: statusKind, streak: 0 });
    } else if (prevStatus && prevStatus.streak + 1 < STATUS_HOLD_SAMPLES) {
      statusText = prevStatus.text;
      statusKind = prevStatus.kind;
      statusHold.set(t.epic, {
        text: prevStatus.text,
        kind: prevStatus.kind,
        streak: prevStatus.streak + 1,
      });
    } else {
      statusHold.set(t.epic, { text: statusText, kind: statusKind, streak: 0 });
    }

    return {
      id: t.id,
      label: t.label,
      epic: t.epic,
      mid,
      ticks: [...(ticks.get(t.epic) ?? [])],
      statusKind,
      statusText,
      profile,
      conviction,
      sniperThreshold: epicGate,
      velocity,
      zScore,
      tpm,
      inActiveStack,
      inPromoted: Boolean(inPromoted),
      rankLane,
      rank:
        rankedRow?.rank != null && Number.isFinite(Number(rankedRow.rank))
          ? Number(rankedRow.rank)
          : null,
      allowEntries,
      regimeLabel,
      maxSpreadPts: assetProf.maxSpreadPts,
      pointMultiplier: assetProf.pointMultiplier,
      isForex: assetProf.isForex,
      openCount: openOn.length,
      openDirection: focusOpen?.direction ?? null,
      pnlGbp: focusOpen?.pnlGbp ?? null,
      trailFloorGbp,
      softLossGbp,
      trailPriceLevel,
    };
  });
}

function buildNodes(
  fulfillment: FulfillmentPayload | null,
  ticks: Map<string, number[]>,
  positions: PositionAuthorityRow[],
  enrich: Enrichment,
  latencyMs: number,
): AssetMarketNode[] {
  const dowQ = liveQuoteForEpic(fulfillment, DOW);
  const crudeQ = liveQuoteForEpic(fulfillment, CRUDE);
  const goldQ = liveQuoteForEpic(fulfillment, GOLD);
  const eurusdQ = liveQuoteForEpic(fulfillment, EURUSD);
  const dowPos = positions.find((p) => p.epic === DOW) ?? positions[0];
  const bias = (enrich.ops?.grok_macro_bias || "NEUTRAL").toUpperCase();
  const crudeMid = midOf(fulfillment, CRUDE);
  const goldMid = midOf(fulfillment, GOLD);
  const eurusdMid = midOf(fulfillment, EURUSD);
  const dowMid = midOf(fulfillment, DOW);

  // VIX risk metric from regime ATR ratio (no file / Yahoo poll)
  const dowReg = enrich.regime?.markets?.find((m) => m.epic === DOW);
  const atrRatio = Number(dowReg?.atr_ratio ?? 1);
  const vixDisplay = Number(
    Math.max(12, Math.min(32, 18 + (atrRatio - 1) * 25)).toFixed(1),
  );

  const pnl = dowPos?.pnlGbp ?? null;
  const pnlTone: AssetMarketNode["secondaryTone"] =
    pnl == null ? "mute" : pnl <= B1_FLOOR ? "bad" : pnl < 0 ? "warn" : "ok";

  const useGold = goldMid > 0;
  const slot3Epic = useGold ? GOLD : EURUSD;
  const slot3Mid = useGold ? goldMid : eurusdMid;
  const slot3Q = useGold ? goldQ : eurusdQ;

  return [
    {
      id: "dow",
      slot: 0,
      label: "WALL ST / DOW Index",
      epic: DOW,
      mid: dowMid,
      bid: dowQ?.bid ?? 0,
      offer: dowQ?.offer ?? 0,
      primaryLabel: "ENTRY",
      primaryValue: fmtPx(dowPos?.entry || 52388.7, DOW),
      secondaryLabel: "UPL",
      secondaryValue: fmtGbp(pnl ?? -122.35),
      secondaryTone: pnlTone,
      chip: `BIAS ${bias}`,
      chipTone: bias === "VETO" ? "bad" : bias === "NEUTRAL" ? "mute" : "ok",
      ticks: [...(ticks.get(DOW) ?? [])],
      tickLatencyMs: latencyMs,
      source: dowQ?.source || "stream",
    },
    {
      id: "brent",
      slot: 1,
      label: "BRENT CRUDE",
      epic: CRUDE,
      mid: crudeMid,
      bid: crudeQ?.bid ?? 0,
      offer: crudeQ?.offer ?? 0,
      primaryLabel: "SPOT USD",
      primaryValue: crudeMid > 0 ? `$${crudeMid.toFixed(2)}` : "$88.00",
      secondaryLabel: "MACRO ALERT",
      secondaryValue:
        crudeMid >= 88 ? "HOT ≥$88" : crudeMid > 0 ? `REF $88 · LIVE` : "$88 GEO",
      secondaryTone: crudeMid >= 88 ? "warn" : "mute",
      chip: "GEO MONITOR",
      chipTone: "warn",
      ticks: [...(ticks.get(CRUDE) ?? [])],
      tickLatencyMs: latencyMs,
      source: crudeQ?.source || "stream",
    },
    {
      id: "vix",
      slot: 2,
      label: "VIX Index",
      epic: DOW,
      mid: vixDisplay,
      bid: 0,
      offer: 0,
      primaryLabel: "RISK METRIC",
      primaryValue: vixDisplay.toFixed(1),
      secondaryLabel: "ATR RATIO",
      secondaryValue: atrRatio.toFixed(3),
      secondaryTone: vixDisplay >= 20 ? "warn" : "ok",
      chip: "VOL GATE",
      chipTone: vixDisplay >= 20 ? "warn" : "ok",
      ticks: [...(ticks.get("__vix__") ?? [])],
      tickLatencyMs: latencyMs,
      source: "regime_proxy",
    },
    {
      id: "gold",
      slot: 3,
      label: useGold ? "GOLD" : "EUR/USD Proxy",
      epic: slot3Epic,
      mid: slot3Mid,
      bid: slot3Q?.bid ?? 0,
      offer: slot3Q?.offer ?? 0,
      primaryLabel: useGold ? "SPOT" : "MID",
      primaryValue: fmtPx(slot3Mid, slot3Epic),
      secondaryLabel: useGold ? "PROXY" : "FX PROXY",
      secondaryValue: useGold ? "XAU" : "EURUSD",
      secondaryTone: "mute",
      chip: useGold ? "MATRIX" : "FX NODE",
      chipTone: "ok",
      ticks: [...(ticks.get(slot3Epic) ?? [])],
      tickLatencyMs: latencyMs,
      source: slot3Q?.source || "stream",
    },
  ];
}

function buildSafety(
  fulfillment: FulfillmentPayload | null,
  live: LivePositionsPayload | null,
  enrich: Enrichment,
  wsState: string,
): QuantumSafetyMatrix {
  const qf = fulfillment?.quote_freshness;
  // Hub quote freshness only — do not require all_ready (stage lights can be
  // yellow while Yahoo hub ages are within the entry budget).
  const quotesFresh = Boolean(qf?.fresh === true);
  const ageSec =
    qf?.age_sec != null && Number.isFinite(Number(qf.age_sec))
      ? Number(qf.age_sec)
      : null;
  const memRaw = String(fulfillment?.memory_alignment || "WARMING");
  // Never display TRUE SYNC when hub quotes are fail-closed.
  const mem = quotesFresh ? memRaw : memRaw === "TRUE SYNC" ? "WARMING" : memRaw;
  const sync = live?.sync_status || "unknown";
  const hubStale =
    fulfillment == null ? true : !quotesFresh || Boolean(live?.stale);
  const bias = (enrich.ops?.grok_macro_bias || "NEUTRAL").toUpperCase();
  const feedOk = wsState === "live" && !hubStale && quotesFresh;
  const feedLabel = feedOk
    ? `LIVE ${ageSec != null ? `${Math.round(ageSec * 1000)}ms` : "OK"}`
    : ageSec != null
      ? `HUB STALE ${ageSec.toFixed(0)}s`
      : wsState === "live"
        ? "WS ONLY · HUB STALE"
        : wsState.toUpperCase();
  return {
    driverIntegrity: feedOk ? "OK" : hubStale ? "DEGRADED" : "FAULT",
    cudaCompiler:
      mem === "TRUE SYNC" ? "READY" : mem === "WARMING" ? "COMPILING" : "ERROR",
    databaseSync:
      sync === "ok" || sync === "fresh" || live?.ok
        ? live?.stale
          ? "LAGGING"
          : "SYNCED"
        : sync === "missing"
          ? "LAGGING"
          : "OFFLINE",
    thermalTrip: bias === "VETO" || live?.verdict === "CRITICAL",
    feedLabel,
    memoryAlignment: mem,
    macroBias: bias,
    lastUpdatedMs: Date.now(),
  };
}

export function useQuantumNodeMemory() {
  const fulfillment = useFulfillment();
  const positionsHook = useOpenPositions();
  const [mounted, setMounted] = useState(false);
  const [view, setView] = useState<QuantumNodeView | null>(null);
  const [wsState, setWsState] = useState("connecting");

  const ticksRef = useRef(new Map<string, number[]>());
  const alphaRef = useRef<AlphaPoint[]>([]);
  const prevMidRef = useRef(0);
  const convictionEmaRef = useRef(new Map<string, number>());
  const convictionHoldRef = useRef(
    new Map<string, { p: number; streak: number }>(),
  );
  const statusHoldRef = useRef(
    new Map<string, { text: string; kind: SniperStatusKind; streak: number }>(),
  );
  const enrichRef = useRef<Enrichment>({
    risk: null,
    tradeState: null,
    tradeSupport: null,
    regime: null,
    ops: null,
    rotation: null,
    sniperMl: null,
  });
  const telemetryRef = useRef<TelemetryPayload | null>(null);
  const latencyRef = useRef(0);
  const tickCount = useRef(0);

  // Ingest fulfillment quotes into tick rings (async memory, not React)
  useEffect(() => {
    const payload = fulfillment.data;
    if (!payload) return;
    const t0 = performance.now();
    for (const epic of [DOW, FTSE, DAX, CRUDE, GOLD, EURUSD]) {
      pushTick(ticksRef.current, epic, midOf(payload, epic));
    }
    latencyRef.current = Math.max(0, performance.now() - t0);
  }, [fulfillment.data]);

  // Enrichment APIs — shared agent endpoints, not file polls.
  // Rotation: dual-fetch :8080+:8081; prefer SB when CFD A2-paused.
  useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      const cfdBase = cfdHttpBase();
      const sbBase = sbHttpBase();
      const results = await Promise.allSettled([
        // Coalesced enrichment — avoid 2.5s × N endpoint storms on the desk API.
        fetchAgentJson<RiskMonitorPayload>("/api/position_risk_monitor", undefined, 3000),
        fetchAgentJson<TradeStatePayload>("/api/trade_state", undefined, 3000),
        fetchAgentJson<TradeSupportPayload>("/api/trade_support/status", undefined, 3000),
        fetchAgentJson<RegimePayload>("/api/regime_state", undefined, 3000),
        fetchAgentJson<OpsStrip>("/api/desk/ops_strip", undefined, 2000),
        fetchDeskJson<RotationPayload>(cfdBase, "/api/rotation_state", undefined, 2500),
        fetchDeskJson<RotationPayload>(sbBase, "/api/rotation_state", undefined, 2500),
        fetchDeskJson<HealthPauseSlice>(cfdBase, "/api/health", undefined, 2000),
        fetchAgentJson<SniperMlPayload>("/api/desk/sniper_ml", undefined, 2500),
      ]);
      if (cancelled) return;
      const [
        risk,
        tradeState,
        tradeSupport,
        regime,
        ops,
        cfdRotation,
        sbRotation,
        cfdHealth,
        sniperMl,
      ] = results;
      const preferSb =
        cfdHealth.status === "fulfilled" &&
        cfdHealth.value?.trading_paused === true;
      const mergedRotation = pickPreferredRotation(
        cfdRotation.status === "fulfilled" ? cfdRotation.value : null,
        sbRotation.status === "fulfilled" ? sbRotation.value : null,
        preferSb,
      );
      enrichRef.current = {
        risk: risk.status === "fulfilled" ? risk.value : enrichRef.current.risk,
        tradeState:
          tradeState.status === "fulfilled"
            ? tradeState.value
            : enrichRef.current.tradeState,
        tradeSupport:
          tradeSupport.status === "fulfilled"
            ? tradeSupport.value
            : enrichRef.current.tradeSupport,
        regime:
          regime.status === "fulfilled" ? regime.value : enrichRef.current.regime,
        ops:
          ops.status === "fulfilled"
            ? ops.value
            : enrichRef.current.ops ?? { grok_macro_bias: "NEUTRAL" },
        rotation: mergedRotation ?? enrichRef.current.rotation,
        sniperMl:
          sniperMl.status === "fulfilled"
            ? sniperMl.value
            : enrichRef.current.sniperMl,
      };
      if (ops.status === "rejected" && !enrichRef.current.ops) {
        enrichRef.current.ops = { grok_macro_bias: "NEUTRAL" };
      }
    };
    void pull();
    // 12s coalesce — enrichment must not storm agent/REST budget with capital
    const id = window.setInterval(pull, 12000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  // WS telemetry — OBI when bridge publishes; otherwise quote proxy
  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;
    let attempt = 0;

    const connect = () => {
      if (cancelled) return;
      try {
        ws = new WebSocket(`${agentWsBase()}/api/telemetry/stream`);
      } catch {
        setWsState("offline");
        timer = setTimeout(connect, 2000);
        return;
      }
      ws.onopen = () => {
        attempt = 0;
        setWsState("live");
      };
      ws.onmessage = (ev) => {
        try {
          telemetryRef.current = JSON.parse(String(ev.data || "{}"));
          setWsState("live");
        } catch {
          /* ignore */
        }
      };
      ws.onerror = () => setWsState("offline");
      ws.onclose = () => {
        if (cancelled) return;
        setWsState("offline");
        attempt += 1;
        timer = setTimeout(connect, Math.min(8000, 500 * 2 ** attempt));
      };
    };
    connect();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    };
  }, []);

  const compose = useCallback(() => {
    const enrich = enrichRef.current;
    const live = positionsHook.data;
    const midByEpic = new Map<string, number>();
    for (const epic of [DOW, FTSE, DAX, CRUDE, GOLD, EURUSD]) {
      const m = midOf(fulfillment.data, epic);
      if (m > 0) midByEpic.set(epic, m);
    }
    const positions = mergePositions(live, enrich, midByEpic);
    const atr = atrFor(enrich.regime, DOW);
    const dowMid = midOf(fulfillment.data, DOW);
    const q = liveQuoteForEpic(fulfillment.data, DOW);
    let obi = 0;
    const tel = telemetryRef.current as Record<string, unknown> | null;
    const obiMap = tel?.order_book_imbalance as
      | Record<string, { ratio?: number; obi_ratio?: number }>
      | undefined;
    const telObi = obiMap?.[DOW]?.ratio ?? obiMap?.[DOW]?.obi_ratio;
    if (typeof telObi === "number") {
      obi = clampObi(telObi);
    } else {
      obi = estimateObi(
        q?.bid ?? 0,
        q?.offer ?? 0,
        dowMid,
        prevMidRef.current,
        atr,
      );
    }
    if (dowMid > 0) {
      alphaRef.current.push({ t: Date.now(), mid: dowMid, obi });
      if (alphaRef.current.length > ALPHA_CAP) {
        alphaRef.current.splice(0, alphaRef.current.length - ALPHA_CAP);
      }
      prevMidRef.current = dowMid;
    }

    // VIX sparkline from risk metric walk
    const nodesPreview = buildNodes(
      fulfillment.data,
      ticksRef.current,
      positions,
      enrich,
      latencyRef.current,
    );
    const vixNode = nodesPreview.find((n) => n.id === "vix");
    if (vixNode) {
      pushTick(ticksRef.current, "__vix__", Number(vixNode.primaryValue));
    }

    tickCount.current += 1;
    const nodes = buildNodes(
      fulfillment.data,
      ticksRef.current,
      positions,
      enrich,
      latencyRef.current,
    );
    const scanner = buildScanner(
      fulfillment.data,
      ticksRef.current,
      positions,
      enrich,
      convictionEmaRef.current,
      convictionHoldRef.current,
      statusHoldRef.current,
    );
    const rankedChrome = buildRankedChrome(enrich.rotation);
    const safety = buildSafety(fulfillment.data, live, enrich, wsState);
    const alphaSeries = alphaRef.current.slice();
    const last = alphaSeries[alphaSeries.length - 1];
    const total =
      enrich.tradeSupport?.total_unrealized_gbp ??
      enrich.risk?.positions?.reduce((a, p) => a + Number(p.pnl_gbp ?? 0), 0) ??
      live?.total_pnl_gbp ??
      null;
    const deskIdleReason =
      enrich.ops?.desk_idle_reason?.label ||
      enrich.ops?.desk_idle_reason?.code ||
      null;
    const focusEpic = rankedChrome.active && enrich.rotation?.rotation?.ranked_rotator?.dominant
      ? String(enrich.rotation.rotation.ranked_rotator.dominant)
      : DOW;
    const focusLabel =
      focusEpic === GOLD
        ? "GOLD"
        : focusEpic === EURUSD
          ? "EUR/USD"
          : focusEpic === FTSE
            ? "FTSE 100"
            : "WALL ST / DOW";

    setView({
      nodes,
      scanner,
      rankedChrome,
      alpha: {
        epic: focusEpic,
        label: focusLabel,
        series: alphaSeries,
        lastMid: last?.mid ?? dowMid,
        lastObi: last?.obi ?? 0,
      },
      positions,
      safety,
      tick: tickCount.current,
      totalUplGbp: total != null ? Number(total) : null,
      wsState,
      deskIdleReason,
    });
  }, [fulfillment.data, positionsHook.data, wsState]);

  useEffect(() => {
    setMounted(true);
    compose();
    const id = window.setInterval(compose, 1000 / UI_HZ);
    return () => window.clearInterval(id);
  }, [compose]);

  const purgeQueues = useCallback(() => {
    alphaRef.current = [];
    ticksRef.current.clear();
    convictionEmaRef.current.clear();
    convictionHoldRef.current.clear();
  }, []);

  return {
    mounted,
    view,
    fulfillment,
    positionsHook,
    purgeQueues,
  };
}
