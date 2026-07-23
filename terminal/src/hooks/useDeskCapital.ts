"use client";

/**
 * Capital accounting plane — ops_strip + /api/trades + Next proxies
 * (journal CSV + learning_db) + client-side open→closed UPL bridge.
 * Browser never touches agent disk.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchAgentJson, type TradesPayload } from "@/lib/agent-client";
import { EPIC_LABELS } from "@/lib/constants";
import type { PositionAuthorityRow } from "@/lib/quantum-node-types";

function openBookFingerprint(positions: PositionAuthorityRow[]): string {
  return positions
    .map(
      (p) =>
        `${p.dealId}:${p.pnlGbp ?? ""}:${p.trailFloorGbp ?? ""}:${p.currentMid ?? ""}`,
    )
    .sort()
    .join("|");
}

function assetLabel(raw: string): string {
  if (!raw) return "";
  return EPIC_LABELS[raw] || raw.split(".")[2] || raw;
}

export type OpsStripPayload = {
  ok?: boolean;
  daily_realized_pnl_gbp?: number;
  daily_milestone_gbp?: number;
  progress_pct?: number;
  atr_reward_risk?: number;
  grok_macro_bias?: string;
  journal?: string;
};

export type LedgerQuality =
  | "settled"
  | "cancelled"
  | "flat"
  | "bridged"
  | "unknown";

export type ClosedLedgerRow = {
  timestamp: string;
  dealId: string;
  direction: string;
  entry: number | null;
  exit: number | null;
  realizedGbp: number | null;
  closingFillRate: number | null;
  market?: string;
  result?: string;
  size?: number | null;
  pnlPoints?: number | null;
  deltaPts?: number | null;
  quality?: LedgerQuality;
  cumulativeGbp?: number | null;
  lastLiveUpl?: number | null;
  softLossGbp?: number | null;
  trailFloorGbp?: number | null;
};

export type AssetStackBar = {
  label: string;
  settledGbp: number;
  openUplGbp: number;
  pct: number;
};

export type DeskCapitalView = {
  realizedTodayGbp: number;
  milestoneGbp: number;
  progressPct: number;
  netCashFlowGbp: number;
  winRate: number | null;
  profitFactor: number | null;
  wins: number;
  losses: number;
  cancelledCount: number;
  settledCount: number;
  bridgedCount: number;
  closedRows: ClosedLedgerRow[];
  recentCashRows: ClosedLedgerRow[];
  /** Settled WIN/LOSS cash path only */
  equitySeries: number[];
  /** Bridged / provisional UPL exposure path (does not alter settled cash) */
  provisionalEquitySeries: number[];
  assetStack: AssetStackBar[];
  bias: string;
  pending: boolean;
  error: string | null;
  dataNote: string | null;
  /** Settled + bridged cash used for mission ETA */
  provisionalCashGbp: number;
  sessionStartMs: number | null;
};

type BridgeSnap = {
  dealId: string;
  epic: string;
  label: string;
  direction: string;
  entry: number;
  size: number;
  pnlGbp: number | null;
  mid: number | null;
  softLossGbp: number | null;
  trailFloorGbp: number | null;
  seenAt: number;
};

const BRIDGE_TTL_MS = 2 * 60 * 60 * 1000;
const BRIDGE_CAP = 32;

function todayPrefix(): string {
  return new Date().toISOString().slice(0, 10);
}

function isToday(ts: string): boolean {
  if (!ts) return false;
  return ts.replace(" ", "T").startsWith(todayPrefix());
}

function signedDeltaPts(
  direction: string,
  entry: number | null,
  exit: number | null,
): number | null {
  if (entry == null || exit == null || !(entry > 0) || !(exit > 0)) return null;
  const raw = exit - entry;
  return direction.toUpperCase() === "SELL" ? -raw : raw;
}

function inferQuality(r: ClosedLedgerRow): LedgerQuality {
  if (r.quality === "bridged") return "bridged";
  if (r.quality) return r.quality;
  const res = String(r.result || "").toUpperCase();
  if (res === "CANCELLED" || res === "REJECTED") return "cancelled";
  if (res === "WIN" || res === "LOSS" || res === "BREAKEVEN") return "settled";
  const flatLevels =
    r.entry != null &&
    r.exit != null &&
    Math.abs(r.entry - r.exit) < 1e-9 &&
    (r.realizedGbp == null || Math.abs(r.realizedGbp) < 1e-9);
  if (flatLevels) return "flat";
  if (r.realizedGbp != null && Number.isFinite(r.realizedGbp)) return "settled";
  return "unknown";
}

function isCashQuality(q: LedgerQuality): boolean {
  return q === "settled" || q === "bridged";
}

function computeStats(rows: ClosedLedgerRow[]) {
  let wins = 0;
  let losses = 0;
  let grossWin = 0;
  let grossLoss = 0;
  let realized = 0;
  let provisional = 0;
  let settledCount = 0;
  let cancelledCount = 0;
  let bridgedCount = 0;
  for (const r of rows) {
    const q = inferQuality(r);
    if (q === "cancelled" || q === "flat") {
      cancelledCount += 1;
      continue;
    }
    if (q === "bridged") {
      bridgedCount += 1;
      if (r.realizedGbp != null) provisional += r.realizedGbp;
      continue;
    }
    const pnl = r.realizedGbp;
    if (pnl == null || Number.isNaN(pnl)) continue;
    settledCount += 1;
    realized += pnl;
    provisional += pnl;
    if (pnl > 0) {
      wins += 1;
      grossWin += pnl;
    } else if (pnl < 0) {
      losses += 1;
      grossLoss += Math.abs(pnl);
    }
  }
  const decided = wins + losses;
  const winRate = decided > 0 ? wins / decided : null;
  const profitFactor =
    grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : null;
  return {
    wins,
    losses,
    realized,
    provisional,
    winRate,
    profitFactor,
    settledCount,
    cancelledCount,
    bridgedCount,
  };
}

function withCumulative(rows: ClosedLedgerRow[]): ClosedLedgerRow[] {
  const chrono = [...rows].reverse();
  let run = 0;
  const stamped = chrono.map((r) => {
    const q = inferQuality(r);
    if (isCashQuality(q) && r.realizedGbp != null) run += r.realizedGbp;
    return {
      ...r,
      quality: q,
      deltaPts:
        r.deltaPts ?? signedDeltaPts(r.direction, r.entry, r.exit ?? r.entry),
      cumulativeGbp: isCashQuality(q) ? run : null,
    };
  });
  return stamped.reverse();
}

/** Settled WIN/LOSS only — never bridges into the primary cash curve. */
function buildSettledEquitySeries(rows: ClosedLedgerRow[]): number[] {
  const chrono = [...rows].reverse();
  const series: number[] = [0];
  let run = 0;
  for (const r of chrono) {
    if (inferQuality(r) === "settled" && r.realizedGbp != null) {
      run += r.realizedGbp;
      series.push(run);
    }
  }
  return series.length > 1 ? series : [0, 0];
}

/** Provisional lane — bridged last-live UPL exposure only. */
function buildProvisionalEquitySeries(rows: ClosedLedgerRow[]): number[] {
  const chrono = [...rows].reverse();
  const series: number[] = [0];
  let run = 0;
  for (const r of chrono) {
    if (inferQuality(r) === "bridged" && r.realizedGbp != null) {
      run += r.realizedGbp;
      series.push(run);
    }
  }
  return series.length > 1 ? series : [0, 0];
}

function buildAssetStack(
  rows: ClosedLedgerRow[],
  openPositions: PositionAuthorityRow[],
): AssetStackBar[] {
  const map = new Map<string, { settled: number; open: number }>();
  for (const r of rows) {
    const q = inferQuality(r);
    if (!isCashQuality(q) || r.realizedGbp == null) continue;
    const key = r.market || "Unknown";
    const cur = map.get(key) ?? { settled: 0, open: 0 };
    cur.settled += r.realizedGbp;
    map.set(key, cur);
  }
  for (const p of openPositions) {
    const key = p.label || assetLabel(p.epic) || "Open";
    const cur = map.get(key) ?? { settled: 0, open: 0 };
    cur.open += p.pnlGbp ?? 0;
    map.set(key, cur);
  }
  const rowsOut = Array.from(map.entries()).map(([label, v]) => ({
    label,
    settledGbp: v.settled,
    openUplGbp: v.open,
    pct: 0,
  }));
  const denom = rowsOut.reduce(
    (a, r) => a + Math.abs(r.settledGbp) + Math.abs(r.openUplGbp),
    0,
  );
  for (const r of rowsOut) {
    r.pct =
      denom > 0
        ? ((Math.abs(r.settledGbp) + Math.abs(r.openUplGbp)) / denom) * 100
        : 0;
  }
  return rowsOut.sort(
    (a, b) =>
      Math.abs(b.settledGbp) +
      Math.abs(b.openUplGbp) -
      (Math.abs(a.settledGbp) + Math.abs(a.openUplGbp)),
  );
}

function pruneBridge(map: Map<string, BridgeSnap>) {
  const now = Date.now();
  for (const [id, snap] of map) {
    if (now - snap.seenAt > BRIDGE_TTL_MS) map.delete(id);
  }
  if (map.size <= BRIDGE_CAP) return;
  const ordered = [...map.entries()].sort((a, b) => a[1].seenAt - b[1].seenAt);
  while (map.size > BRIDGE_CAP && ordered.length) {
    const [id] = ordered.shift()!;
    map.delete(id);
  }
}

export function useDeskCapital(
  openUplGbp: number | null,
  openPositions: PositionAuthorityRow[] = [],
): DeskCapitalView {
  const bridgeRef = useRef(new Map<string, BridgeSnap>());
  const prevOpenRef = useRef(new Set<string>());
  const sessionStartRef = useRef<number | null>(null);
  const openFp = useMemo(
    () => openBookFingerprint(openPositions),
    [openPositions],
  );
  const openRef = useRef(openPositions);
  openRef.current = openPositions;

  const [view, setView] = useState<DeskCapitalView>({
    realizedTodayGbp: 0,
    milestoneGbp: 1000,
    progressPct: 0,
    netCashFlowGbp: 0,
    winRate: null,
    profitFactor: null,
    wins: 0,
    losses: 0,
    cancelledCount: 0,
    settledCount: 0,
    bridgedCount: 0,
    closedRows: [],
    recentCashRows: [],
    equitySeries: [0, 0],
    provisionalEquitySeries: [0, 0],
    assetStack: [],
    bias: "NEUTRAL",
    pending: true,
    error: null,
    dataNote: null,
    provisionalCashGbp: 0,
    sessionStartMs: null,
  });

  // Track open→closed transitions into bridge memory
  useEffect(() => {
    const positions = openRef.current;
    const liveIds = new Set(positions.map((p) => p.dealId).filter(Boolean));
    const prev = prevOpenRef.current;
    for (const p of positions) {
      if (!p.dealId) continue;
      bridgeRef.current.set(p.dealId, {
        dealId: p.dealId,
        epic: p.epic,
        label: p.label,
        direction: p.direction,
        entry: p.entry,
        size: p.size,
        pnlGbp: p.pnlGbp,
        mid: p.currentMid,
        softLossGbp: p.softLossGbp,
        trailFloorGbp: p.trailFloorGbp,
        seenAt: Date.now(),
      });
    }
    for (const id of prev) {
      if (!liveIds.has(id)) {
        const snap = bridgeRef.current.get(id);
        if (snap) snap.seenAt = Date.now();
      }
    }
    prevOpenRef.current = liveIds;
    pruneBridge(bridgeRef.current);
  }, [openFp]);

  useEffect(() => {
    let cancelled = false;

    const pull = async () => {
      try {
        const openPositions = openRef.current;
        const [ops, trades, journal, learning] = await Promise.all([
          fetchAgentJson<OpsStripPayload>("/api/desk/ops_strip", undefined, 2500),
          fetchAgentJson<TradesPayload>("/api/trades?limit=128", undefined, 3500),
          fetch("/api/desk/journal", { cache: "no-store" })
            .then((r) => r.json())
            .catch(() => ({ ok: false, rows: [] })),
          fetch("/api/desk/closed_ledger", { cache: "no-store" })
            .then((r) => r.json())
            .catch(() => ({ ok: false, rows: [] })),
        ]);
        if (cancelled) return;

        const learningRows: ClosedLedgerRow[] = Array.isArray(learning?.rows)
          ? learning.rows.map(
              (r: {
                timestamp?: string;
                dealId?: string;
                direction?: string;
                entry?: number | null;
                exit?: number | null;
                realizedGbp?: number | null;
                market?: string;
                result?: string;
                size?: number | null;
                pnlPoints?: number | null;
                quality?: LedgerQuality;
              }) => ({
                timestamp: String(r.timestamp || ""),
                dealId: String(r.dealId || ""),
                direction: String(r.direction || ""),
                entry: r.entry ?? null,
                exit: r.exit ?? null,
                realizedGbp: r.realizedGbp ?? null,
                closingFillRate: null,
                market: assetLabel(String(r.market || "")),
                result: String(r.result || ""),
                size: r.size ?? null,
                pnlPoints: r.pnlPoints ?? null,
                deltaPts: signedDeltaPts(
                  String(r.direction || ""),
                  r.entry ?? null,
                  r.exit ?? null,
                ),
                quality: r.quality,
              }),
            )
          : [];

        const journalByDeal = new Map<string, ClosedLedgerRow>();
        if (Array.isArray(journal?.rows)) {
          for (const r of journal.rows as Array<Record<string, unknown>>) {
            const dealId = String(r.dealId || "");
            if (!dealId) continue;
            journalByDeal.set(dealId, {
              timestamp: String(r.timestamp || ""),
              dealId,
              direction: String(r.direction || ""),
              entry: (r.entry as number | null) ?? null,
              exit: (r.exit as number | null) ?? null,
              realizedGbp: (r.realizedGbp as number | null) ?? null,
              closingFillRate: (r.closingFillRate as number | null) ?? null,
            });
          }
        }

        const tradeClosed = (trades.closed ?? trades.trades ?? []) as Array<
          Record<string, unknown>
        >;
        const fromTradesAll: ClosedLedgerRow[] = tradeClosed
          .map((t) => {
            const result = String(t.result || "").toUpperCase();
            const pnl =
              t.pnl_gbp != null && Number.isFinite(Number(t.pnl_gbp))
                ? Number(t.pnl_gbp)
                : null;
            const direction = String(t.direction || "").toUpperCase();
            const entry =
              t.entry != null && Number.isFinite(Number(t.entry))
                ? Number(t.entry)
                : null;
            const exit =
              t.exit != null && Number.isFinite(Number(t.exit))
                ? Number(t.exit)
                : null;
            return {
              timestamp: String(t.closed_at || t.timestamp || ""),
              dealId: String(t.deal_id || t.dealId || ""),
              direction,
              entry,
              exit,
              realizedGbp: pnl,
              closingFillRate: null,
              market: assetLabel(String(t.market || t.epic || "")),
              result,
              deltaPts: signedDeltaPts(direction, entry, exit),
              quality: inferQuality({
                timestamp: "",
                dealId: "",
                direction,
                entry,
                exit,
                realizedGbp: pnl,
                closingFillRate: null,
                result,
              }),
            };
          })
          .filter((r) => r.dealId);

        let closedRows: ClosedLedgerRow[] =
          learningRows.length > 0
            ? learningRows.map((r) => {
                const j = journalByDeal.get(r.dealId);
                return {
                  ...r,
                  closingFillRate: j?.closingFillRate ?? r.closingFillRate,
                  direction: r.direction || j?.direction || "",
                  market: r.market || j?.market || "",
                  quality: inferQuality(r),
                };
              })
            : Array.from(journalByDeal.values()).map((r) => ({
                ...r,
                quality: inferQuality(r),
              }));

        if (closedRows.length === 0) {
          closedRows = fromTradesAll.filter((r) => isToday(r.timestamp));
        }

        // True settled WIN/LOSS today — gate for bridge / equity truth
        const sourceSettledToday = closedRows.filter((r) => {
          const q = inferQuality(r);
          return q === "settled" && (!r.timestamp || isToday(r.timestamp));
        }).length;
        const dailyTxCount = sourceSettledToday;

        // Bridge only when we already have real settled cash today.
        // Zero-tx days must not invent BRIDGED phantoms or stale equity curves.
        const liveIds = new Set(openPositions.map((p) => p.dealId));
        pruneBridge(bridgeRef.current);
        const closedIds = new Set(closedRows.map((r) => r.dealId));
        if (dailyTxCount > 0) {
          closedRows = closedRows.map((r) => {
            const snap = bridgeRef.current.get(r.dealId);
            const q = inferQuality(r);
            if (
              snap &&
              !liveIds.has(r.dealId) &&
              (q === "cancelled" || q === "flat" || r.realizedGbp == null) &&
              snap.pnlGbp != null
            ) {
              return {
                ...r,
                quality: "bridged" as const,
                result: r.result || "BRIDGED",
                realizedGbp: snap.pnlGbp,
                lastLiveUpl: snap.pnlGbp,
                exit: r.exit ?? snap.mid,
                direction: r.direction || snap.direction,
                market: r.market || snap.label,
                size: r.size ?? snap.size,
                softLossGbp: snap.softLossGbp,
                trailFloorGbp: snap.trailFloorGbp,
                deltaPts: signedDeltaPts(
                  r.direction || snap.direction,
                  r.entry ?? snap.entry,
                  r.exit ?? snap.mid,
                ),
              };
            }
            return r;
          });

          for (const [id, snap] of bridgeRef.current) {
            if (liveIds.has(id) || closedIds.has(id)) continue;
            if (snap.pnlGbp == null) continue;
            closedRows.unshift({
              timestamp: new Date(snap.seenAt).toISOString(),
              dealId: id,
              direction: snap.direction,
              entry: snap.entry,
              exit: snap.mid,
              realizedGbp: snap.pnlGbp,
              closingFillRate: null,
              market: snap.label,
              result: "BRIDGED",
              size: snap.size,
              deltaPts: signedDeltaPts(snap.direction, snap.entry, snap.mid),
              quality: "bridged",
              lastLiveUpl: snap.pnlGbp,
              softLossGbp: snap.softLossGbp,
              trailFloorGbp: snap.trailFloorGbp,
            });
          }
        } else {
          // Strip any bridged phantoms so labels / curve stay at true £0.00
          closedRows = closedRows
            .map((r) =>
              inferQuality(r) === "bridged"
                ? {
                    ...r,
                    quality: "cancelled" as const,
                    result: "CANCELLED",
                    realizedGbp: null,
                    lastLiveUpl: null,
                  }
                : r,
            )
            .filter((r) => inferQuality(r) !== "bridged");
        }

        closedRows = withCumulative(closedRows).slice(0, 100);
        let equitySeries = buildSettledEquitySeries(closedRows);
        let provisionalEquitySeries = buildProvisionalEquitySeries(closedRows);
        const assetStack = buildAssetStack(closedRows, openPositions);

        const stats = computeStats(closedRows);
        // Prefer ledger truth; never keep a stale ops_strip negative when tx count is 0
        let realizedOps =
          stats.settledCount > 0 || stats.bridgedCount > 0
            ? Number(
                ops.daily_realized_pnl_gbp != null &&
                  Number.isFinite(Number(ops.daily_realized_pnl_gbp))
                  ? ops.daily_realized_pnl_gbp
                  : stats.realized,
              )
            : stats.realized;
        // Only wipe phantom BRIDGED equity when there is truly no settled cash
        // AND ops_strip agrees. Never silence a non-zero journal / ops realized.
        const opsRealized = Number(ops.daily_realized_pnl_gbp ?? 0);
        if (
          dailyTxCount === 0 &&
          stats.bridgedCount === 0 &&
          stats.settledCount === 0 &&
          Math.abs(opsRealized) < 1e-9 &&
          Math.abs(stats.realized) < 1e-9
        ) {
          realizedOps = 0;
          equitySeries = [0, 0];
          provisionalEquitySeries = [0, 0];
          stats.bridgedCount = 0;
          stats.provisional = 0;
          stats.realized = 0;
        }
        const milestone = Number(ops.daily_milestone_gbp ?? 1000);
        const progress =
          Math.abs(realizedOps) < 1e-9
            ? 0
            : ops.progress_pct != null && Number(ops.progress_pct) > 0
              ? Number(ops.progress_pct)
              : Math.min(100, Math.max(0, (realizedOps / milestone) * 100));
        const upl = openUplGbp ?? 0;

        if (sessionStartRef.current == null && closedRows.length > 0) {
          const oldest = [...closedRows]
            .map((r) => Date.parse(r.timestamp.replace(" ", "T")))
            .filter((t) => Number.isFinite(t))
            .sort((a, b) => a - b)[0];
          if (oldest) sessionStartRef.current = oldest;
        }

        const recentCashRows = fromTradesAll
          .filter(
            (r) =>
              !isToday(r.timestamp) &&
              r.realizedGbp != null &&
              Math.abs(r.realizedGbp) > 0.005,
          )
          .slice(0, 24);

        let dataNote: string | null = null;
        if (stats.bridgedCount > 0 && stats.settledCount === 0) {
          dataNote =
            "Showing last live UPL bridge for closes that journaled as CANCELLED/flat. True WIN/LOSS cash replaces bridge when broker settles.";
        } else if (stats.settledCount === 0 && stats.cancelledCount > 0) {
          dataNote =
            "Session closes are CANCELLED/flat stubs (entry=exit). Realized cash appears when broker fills settle as WIN/LOSS.";
        } else if (stats.settledCount === 0 && recentCashRows.length > 0) {
          dataNote =
            "No settled cash today — showing recent historical closes with P&L below.";
        }

        setView({
          realizedTodayGbp: realizedOps,
          milestoneGbp: milestone,
          progressPct: progress,
          netCashFlowGbp: realizedOps + upl,
          winRate: stats.winRate,
          profitFactor:
            stats.profitFactor != null && Number.isFinite(stats.profitFactor)
              ? stats.profitFactor
              : stats.profitFactor === Infinity
                ? 99
                : null,
          wins: stats.wins,
          losses: stats.losses,
          cancelledCount: stats.cancelledCount,
          settledCount: stats.settledCount,
          bridgedCount: stats.bridgedCount,
          closedRows,
          recentCashRows,
          equitySeries,
          provisionalEquitySeries,
          assetStack,
          bias: String(ops.grok_macro_bias || "NEUTRAL"),
          pending: false,
          error: null,
          dataNote,
          provisionalCashGbp: stats.provisional,
          sessionStartMs: sessionStartRef.current,
        });
      } catch (e) {
        if (!cancelled) {
          setView((prev) => ({
            ...prev,
            pending: false,
            error: e instanceof Error ? e.message : "capital fetch failed",
          }));
        }
      }
    };

    void pull();
    // 8s — share cadence with enrichment; avoid capital+ops REST fanout
    const id = window.setInterval(pull, 8000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [openUplGbp, openFp]);

  return view;
}
