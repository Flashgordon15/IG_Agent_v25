"use client";

/**
 * Desk Intent strip — answers the three pilot questions only:
 * 1. Can we trade? (per-engine ARMED | PAUSED | BLOCKED)
 * 2. Confidence on the focus market? (explicit per account)
 * 3. Is rotation occurring?
 *
 * Ranked rotator: market focus = dominant; promoted set + rotation line
 * from `/api/rotation_state` (prefer SB when CFD A2-paused).
 *
 * When CFD is A2-paused, confidence MUST NOT say "cfd sniper" / imply CFD SETUP.
 */

import { useEffect, useRef, useState, type MutableRefObject } from "react";
import {
  cfdHttpBase,
  fetchDeskJson,
  sbHttpBase,
} from "@/lib/desk-api-bases";
import {
  buildDeskIntentView,
  type DeskIntentHealthSlice,
  type DeskIntentOpsSlice,
  type DeskIntentRotationSlice,
  type DeskIntentSniperByEpic,
  type DeskIntentState,
  type DeskIntentView,
} from "@/lib/desk-intent";
import type { GpuExecutionBuffer } from "@/lib/gpu-execution-buffer";

type Props = {
  bufferRef?: MutableRefObject<GpuExecutionBuffer>;
  cfdOnline?: boolean;
  sbOnline?: boolean;
  pollMs?: number;
};

const EMPTY: DeskIntentView = {
  engines: [
    {
      id: "cfd",
      shortLabel: "CFD",
      origin: "QUANT_SNIPER",
      state: "BLOCKED",
      why: "awaiting ports",
    },
    {
      id: "sb",
      shortLabel: "SB",
      origin: "MACRO_SENTINEL",
      state: "BLOCKED",
      why: "awaiting ports",
    },
  ],
  focusMarket: "—",
  focusEpic: null,
  promotedMarkets: "—",
  rankedActive: false,
  confidenceBand: "—",
  confidencePct: null,
  confidenceSource: null,
  setupMode: null,
  confidenceAccounts: [
    {
      id: "cfd",
      line: "CFD: —",
      band: "—",
      pct: null,
      mode: null,
      source: "CFD sniper",
      suppressed: true,
    },
    {
      id: "sb",
      line: "SB: —",
      band: "—",
      pct: null,
      mode: null,
      source: "SB macro",
      suppressed: true,
    },
  ],
  marketHierarchy: null,
  preferEpic: null,
  preferMarket: null,
  preferenceReason: null,
  rotationKind: "off",
  rotationLabel: "off",
  nextBlock: null,
  sbPostureHint: null,
};

/** Hold band flips until two consecutive polls agree (reduces flicker). */
function debounceConfidenceView(
  prev: DeskIntentView,
  next: DeskIntentView,
  pendingRef: { current: DeskIntentView | null },
): DeskIntentView {
  if (next.confidencePct == null || prev.confidencePct == null) {
    pendingRef.current = null;
    return next;
  }
  const bandFlip =
    prev.confidenceBand !== "—" &&
    next.confidenceBand !== "—" &&
    prev.confidenceBand !== next.confidenceBand;
  const sourceFlip =
    prev.confidenceSource != null &&
    next.confidenceSource != null &&
    prev.confidenceSource !== next.confidenceSource;
  if (!bandFlip && !sourceFlip) {
    pendingRef.current = null;
    return next;
  }
  const pend = pendingRef.current;
  if (
    pend &&
    pend.confidenceBand === next.confidenceBand &&
    pend.confidenceSource === next.confidenceSource
  ) {
    pendingRef.current = null;
    return next;
  }
  pendingRef.current = next;
  // Hold primary confidence only — always refresh per-account lines (truth).
  return {
    ...next,
    confidenceBand: prev.confidenceBand,
    confidencePct: prev.confidencePct,
    confidenceSource: prev.confidenceSource,
    setupMode: prev.setupMode,
  };
}

function stateTone(state: DeskIntentState): string {
  if (state === "ARMED") return "armed";
  if (state === "PAUSED") return "paused";
  return "blocked";
}

function fmtPct(p: number | null): string {
  if (p == null || !Number.isFinite(p)) return "";
  return `${(p * 100).toFixed(0)}%`;
}

async function softFetch<T>(
  base: string,
  path: string,
): Promise<{ ok: boolean; data: T | null }> {
  try {
    const data = await fetchDeskJson<T>(base, path, undefined, 2200);
    return { ok: true, data };
  } catch {
    return { ok: false, data: null };
  }
}

export function DeskIntentStrip({
  bufferRef,
  cfdOnline,
  sbOnline,
  pollMs = 6000,
}: Props) {
  const [view, setView] = useState<DeskIntentView>(EMPTY);
  const pendingConfRef = useRef<DeskIntentView | null>(null);

  useEffect(() => {
    let alive = true;
    const cfdBase = cfdHttpBase();
    const sbBase = sbHttpBase();

    const pull = async () => {
      const [
        cfdHealth,
        sbHealth,
        cfdOps,
        sbOps,
        cfdRot,
        sbRot,
        sbSniper,
        cfdSniper,
      ] = await Promise.all([
        softFetch<DeskIntentHealthSlice>(cfdBase, "/api/health"),
        softFetch<DeskIntentHealthSlice>(sbBase, "/api/health"),
        softFetch<DeskIntentOpsSlice>(cfdBase, "/api/desk/ops_strip"),
        softFetch<DeskIntentOpsSlice>(sbBase, "/api/desk/ops_strip"),
        softFetch<DeskIntentRotationSlice>(cfdBase, "/api/rotation_state"),
        softFetch<DeskIntentRotationSlice>(sbBase, "/api/rotation_state"),
        // Live per-epic confidence — works before agent reload of ranked rotator.
        softFetch<DeskIntentSniperByEpic>(sbBase, "/api/desk/sniper_ml"),
        softFetch<DeskIntentSniperByEpic>(cfdBase, "/api/desk/sniper_ml"),
      ]);

      if (!alive) return;

      const next = buildDeskIntentView({
        cfdOnline: cfdOnline ?? cfdHealth.ok,
        sbOnline: sbOnline ?? sbHealth.ok,
        cfdHealth: cfdHealth.data,
        sbHealth: sbHealth.data,
        cfdOps: cfdOps.data,
        sbOps: sbOps.data,
        cfdRot: cfdRot.data,
        sbRot: sbRot.data,
        bufferFocus: bufferRef?.current?.focusEpic ?? null,
        sbSniperByEpic: sbSniper.data,
        cfdSniperByEpic: cfdSniper.data,
      });
      setView((prev) => debounceConfidenceView(prev, next, pendingConfRef));
    };

    void pull();
    const id = window.setInterval(pull, pollMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [bufferRef, cfdOnline, sbOnline, pollMs]);

  const primaryLine =
    view.confidenceBand === "—" && view.confidencePct == null
      ? "—"
      : [
          view.confidenceBand,
          view.confidencePct != null ? fmtPct(view.confidencePct) : "",
          view.setupMode ? `· ${view.setupMode}` : "",
          view.confidenceSource ? `· ${view.confidenceSource}` : "",
        ]
          .filter(Boolean)
          .join(" ")
          .replace(/\s+/g, " ")
          .trim();

  return (
    <section className="desk-intent" aria-label="Desk intent">
      <header className="desk-intent-head">
        <p className="gpu-kicker">Desk Intent</p>
        <div className="desk-intent-head-right">
          {view.rankedActive ? (
            <span className="desk-intent-ranked-chip" title="Ranked multi-market rotator active">
              Ranked rotator ON
            </span>
          ) : null}
          <span className="desk-intent-mission gpu-ledger-mono">
            trade? · confidence · rotation
          </span>
        </div>
      </header>

      <div className="desk-intent-engines" role="list">
        {view.engines.map((eng) => (
          <div
            key={eng.id}
            className="desk-intent-engine"
            role="listitem"
            data-state={stateTone(eng.state)}
          >
            <span className="desk-intent-engine-id gpu-ledger-mono">
              {eng.shortLabel}{" "}
              <span className="desk-intent-origin">{eng.origin}</span>
            </span>
            <span
              className="desk-intent-state"
              data-tone={stateTone(eng.state)}
            >
              {eng.state}
            </span>
            {eng.why ? (
              <span className="desk-intent-why gpu-ledger-mono">{eng.why}</span>
            ) : null}
          </div>
        ))}
      </div>

      <div className="desk-intent-shared" data-ranked={view.rankedActive ? "on" : "off"}>
        <div className="desk-intent-cell">
          <span className="gpu-metric-key">
            {view.rankedActive || view.preferMarket
              ? "Market focus · prefer"
              : "Market focus"}
          </span>
          <span className="desk-intent-value">
            {view.preferMarket && view.preferMarket !== "—"
              ? view.preferMarket
              : view.focusMarket}
          </span>
        </div>
        <div className="desk-intent-cell">
          <span className="gpu-metric-key">Promoted</span>
          <span
            className="desk-intent-value"
            data-promoted={view.rankedActive ? "on" : "off"}
            title={
              view.rankedActive
                ? "Ranked top-N allowlist (not DOW-only)"
                : "Ranked rotator inactive"
            }
          >
            {view.promotedMarkets}
          </span>
        </div>
        <div className="desk-intent-cell desk-intent-cell--confidence">
          <span className="gpu-metric-key">Confidence</span>
          {view.marketHierarchy ? (
            <span
              className="desk-intent-value desk-intent-hierarchy gpu-ledger-mono"
              title={view.preferenceReason || "Ranked market confidence"}
            >
              {view.marketHierarchy}
            </span>
          ) : (
            <span
              className="desk-intent-value desk-intent-conf-primary"
              data-band={view.confidenceBand}
              data-source={view.confidenceSource ?? undefined}
              title={
                view.confidenceSource
                  ? `Primary: ${view.confidenceSource} (armed / prefer account)`
                  : "No primary sniper score"
              }
            >
              {primaryLine}
            </span>
          )}
          <div className="desk-intent-conf-accounts" role="list">
            {view.confidenceAccounts.map((row) => (
              <span
                key={row.id}
                className="desk-intent-conf-account gpu-ledger-mono"
                role="listitem"
                data-id={row.id}
                data-suppressed={row.suppressed ? "1" : "0"}
                data-band={row.band}
                title={
                  row.suppressed
                    ? `${row.id.toUpperCase()} entries frozen — score suppressed`
                    : `Source: ${row.source ?? "—"}`
                }
              >
                {row.line}
              </span>
            ))}
          </div>
        </div>
        <div className="desk-intent-cell desk-intent-cell--rotation">
          <span className="gpu-metric-key">Rotation</span>
          <span
            className="desk-intent-value desk-intent-rotation-line"
            data-rot={view.rotationKind}
            title={view.rotationLabel}
          >
            {view.rotationLabel}
          </span>
        </div>
      </div>

      {view.nextBlock ? (
        <p className="desk-intent-next gpu-ledger-mono">
          Next block · {view.nextBlock}
        </p>
      ) : null}
    </section>
  );
}
