"use client";

import { useRef } from "react";
import {
  resolveQuoteBudgetMs,
  resolveTelemetryFreezeMs,
  isBootLatencyBufferActive,
} from "@/lib/desk-multiplex";

type Props = {
  isHealthy: boolean;
  quoteAgeMs: number | null;
  operational: boolean;
  /** Wall-clock ms since last multiplex tick — TELEMETRY LOSS when over freeze budget. */
  feedBlockageMs?: number | null;
  /** Unix seconds (or ms) when desk boot/hydration started — enables 30s latency buffer. */
  bootStartedAtSec?: number | null;
  /** Server-provided boot latency buffer seconds (default 30). */
  bootLatencyBufferSec?: number | null;
  tradingPathLive?: boolean;
  tradingPathBadge?: string | null;
  tradingPathReason?: string | null;
  deskRag?: "R" | "A" | "G" | string | null;
  deskRagLabel?: string | null;
  stabilityGrade?: string | null;
  sotOk?: boolean | null;
  sotCount?: number | null;
  livenessOk?: boolean | null;
  bookFlat?: boolean;
  feedLabel?: string | null;
  feedTransport?: string | null;
  dualEngineMode?: boolean;
  dualEngineOperational?: boolean;
  cfdPortOnline?: boolean;
  sbPortOnline?: boolean;
  /** v33 orchestrator self-heal in progress */
  healingActive?: boolean;
  /** CORE_DETACHED=TRUE — maintenance detachment; surveillance only */
  coreDetached?: boolean;
};

/**
 * Top-center massive status — dual-engine port truth + composite R/A/G.
 * Emerald only when both :8080 and :8081 healthy within adaptive quote budgets.
 */
export function SovereignSystemBadge({
  isHealthy,
  quoteAgeMs,
  operational,
  feedBlockageMs,
  bootStartedAtSec,
  bootLatencyBufferSec,
  tradingPathLive,
  tradingPathBadge,
  tradingPathReason,
  deskRag,
  deskRagLabel,
  stabilityGrade,
  sotOk,
  sotCount,
  livenessOk,
  bookFlat,
  feedLabel,
  feedTransport,
  dualEngineMode,
  dualEngineOperational,
  cfdPortOnline,
  sbPortOnline,
  healingActive,
  coreDetached,
}: Props) {
  const maintenanceHeadlineRef = useRef(
    "[🛠️ MAINTENANCE DEVELOPMENT MODE - TRADING DETACHED]",
  );
  const quoteBudgetMs = resolveQuoteBudgetMs(feedTransport ?? feedLabel);
  const telemetryFreezeMs = resolveTelemetryFreezeMs(
    bootStartedAtSec,
    bootLatencyBufferSec,
  );
  const bootBuffer = isBootLatencyBufferActive(
    bootStartedAtSec,
    bootLatencyBufferSec,
  );
  const quoteFresh =
    quoteAgeMs != null &&
    Number.isFinite(quoteAgeMs) &&
    quoteAgeMs <= quoteBudgetMs;
  const telemetryFrozen =
    !bootBuffer &&
    feedBlockageMs != null &&
    Number.isFinite(feedBlockageMs) &&
    feedBlockageMs > telemetryFreezeMs;
  const pathKnown = tradingPathLive !== undefined;
  const pathLive = Boolean(tradingPathLive);
  const stab = String(stabilityGrade || deskRag || (pathLive ? "G" : "A")).toUpperCase();
  const rag = String(deskRag || stab || (pathLive ? "G" : "A")).toUpperCase();

  const live = dualEngineMode
    ? Boolean(dualEngineOperational) && !telemetryFrozen
    : !telemetryFrozen &&
      rag === "G" &&
      stab === "G" &&
      (pathKnown
        ? pathLive && (operational || isHealthy) && quoteFresh
        : operational || (isHealthy && quoteFresh));

  // During boot hydration buffer, suppress false port-offline ENGINE BLOCKAGE.
  const portOffline =
    !bootBuffer && dualEngineMode && (!cfdPortOnline || !sbPortOnline);

  const reconciledFlat =
    bookFlat ??
    ((sotCount == null || sotCount === 0) && sotOk !== false && livenessOk !== false);

  const headline = coreDetached
    ? maintenanceHeadlineRef.current
    : telemetryFrozen
    ? "[🚨 TELEMETRY LOSS - SYSTEM OFFLINE]"
    : healingActive
      ? "[⚠️ AI ENGINE RECOVERY & SELF-HEALING ACTIVE]"
      : bootBuffer && !live
        ? "[🟡 SYSTEM ARMING — BOOT LATENCY BUFFER]"
      : live && reconciledFlat
        ? "[✅ SYSTEM OPERATIONAL & ACTIVE]"
        : dualEngineMode
          ? live
            ? "[✅ DUAL-ENGINE OPERATIONAL & ACTIVE]"
            : portOffline
              ? "[🚨 ENGINE BLOCKAGE - PORT OFFLINE]"
              : "[🟡 DUAL-ENGINE DEGRADED]"
          : live
            ? "[✅ SYSTEM OPERATIONAL & TRADING PATH LIVE]"
            : stab === "R" || rag === "R"
              ? `[🔴 ${deskRagLabel || tradingPathBadge || "DESK CRITICAL"}]`
              : pathKnown
                ? `[🚨 ${tradingPathBadge || deskRagLabel || "DESK TRADING DOWN — ENTRY BLOCKED"}]`
                : isHealthy
                  ? "[🚨 DESK TRADING DOWN — ENTRY PATH UNKNOWN]"
                  : "[🚨 TELEMETRY LOSS - SYSTEM OFFLINE]";

  const reason =
    telemetryFrozen
      ? `FEED BLOCKED > ${telemetryFreezeMs}ms`
      : bootBuffer
        ? `BOOT BUFFER ${Math.round(telemetryFreezeMs / 1000)}s`
      : !live && dualEngineMode
      ? !cfdPortOnline
        ? "CFD :8080 OFFLINE"
        : !sbPortOnline
          ? "SB :8081 OFFLINE"
          : !quoteFresh
            ? `QUOTE STALE > ${quoteBudgetMs}ms`
            : deskRagLabel || tradingPathReason || tradingPathBadge
      : !live && (deskRagLabel || tradingPathReason || tradingPathBadge)
        ? String(deskRagLabel || tradingPathReason || tradingPathBadge)
        : null;

  const toneClass = coreDetached
    ? "sovereign-status sovereign-status--maintenance"
    : healingActive
    ? "sovereign-status sovereign-status--down sovereign-status--amber"
    : live
      ? "sovereign-status sovereign-status--ok"
      : telemetryFrozen || (dualEngineMode && !live)
        ? "sovereign-status sovereign-status--down sovereign-status--red"
        : stab === "R" || rag === "R"
          ? "sovereign-status sovereign-status--down sovereign-status--red"
          : "sovereign-status sovereign-status--down sovereign-status--amber";

  return (
    <div className="sovereign-status-wrap" aria-live="polite">
      <div className={toneClass} role="status">
        <span className="sovereign-rag" data-rag={live ? "G" : stab} aria-hidden>
          ●
        </span>{" "}
        {headline}
      </div>
      <p className="sovereign-status-meta gpu-ledger-mono">
        STAB {stab}
        {" · "}
        RAG {rag}
        {" · "}
        {quoteAgeMs != null && Number.isFinite(quoteAgeMs)
          ? `QUOTE ${Math.round(quoteAgeMs)}ms / ${quoteBudgetMs}ms`
          : "QUOTE —"}
        {" · "}
        {isHealthy ? "HEALTH.OK" : "HEALTH.FAIL"}
        {" · "}
        {dualEngineMode
          ? `CFD ${cfdPortOnline ? "8080✓" : "8080↓"} · SB ${sbPortOnline ? "8081✓" : "8081↓"}`
          : pathKnown
            ? pathLive
              ? "PATH LIVE"
              : "PATH DOWN"
            : "PATH —"}
        {" · "}
        SoT {sotCount != null ? sotCount : "—"}
        {sotOk === false ? " STALE" : sotOk === true ? " OK" : ""}
        {" · "}
        LIV {livenessOk === false ? "↓" : livenessOk === true ? "✓" : "—"}
        {feedLabel ? ` · ${feedLabel}` : ""}
        {reason ? ` · ${reason}` : ""}
      </p>
    </div>
  );
}
