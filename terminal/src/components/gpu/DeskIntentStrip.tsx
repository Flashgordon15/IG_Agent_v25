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
  applyDeskIntentHold,
  buildDeskIntentView,
  initialDeskIntentHoldState,
  type DeskIntentHealthSlice,
  type DeskIntentHoldState,
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

/**
 * Hold band/source flips until two consecutive polls agree, then apply
 * time-based SETUP hold + hierarchy debounce (see applyDeskIntentHold).
 */
function debounceConfidenceView(
  prev: DeskIntentView,
  next: DeskIntentView,
  pendingRef: { current: DeskIntentView | null },
  holdRef: { current: DeskIntentHoldState },
  nowMs: number,
): DeskIntentView {
  let staged = next;
  if (next.confidencePct != null && prev.confidencePct != null) {
    const bandFlip =
      prev.confidenceBand !== "—" &&
      next.confidenceBand !== "—" &&
      prev.confidenceBand !== next.confidenceBand;
    const sourceFlip =
      prev.confidenceSource != null &&
      next.confidenceSource != null &&
      prev.confidenceSource !== next.confidenceSource;
    if (bandFlip || sourceFlip) {
      const pend = pendingRef.current;
      if (
        !(
          pend &&
          pend.confidenceBand === next.confidenceBand &&
          pend.confidenceSource === next.confidenceSource
        )
      ) {
        pendingRef.current = next;
        staged = {
          ...next,
          confidenceBand: prev.confidenceBand,
          confidencePct: prev.confidencePct,
          confidenceSource: prev.confidenceSource,
          setupMode: prev.setupMode,
        };
      } else {
        pendingRef.current = null;
      }
    } else {
      pendingRef.current = null;
    }
  } else {
    pendingRef.current = null;
  }

  const held = applyDeskIntentHold({
    nowMs,
    rawSetupMode: staged.setupMode,
    rawHierarchy: staged.marketHierarchy,
    prev: holdRef.current,
  });
  holdRef.current = held.state;

  let setupMode = held.setupMode;
  let confidenceBand = staged.confidenceBand;
  // If we are still holding before SETUP promotion, don't show High·SETUP.
  if (staged.setupMode === "SETUP" && setupMode !== "SETUP") {
    if (confidenceBand === "High") confidenceBand = "Med";
  }
  if (setupMode === "SETUP" && staged.setupMode === "SETUP") {
    confidenceBand = staged.confidenceBand;
  }

  return {
    ...staged,
    setupMode,
    confidenceBand,
    marketHierarchy: held.marketHierarchy,
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
  const holdRef = useRef<DeskIntentHoldState>(initialDeskIntentHoldState());

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
      setView((prev) =>
        debounceConfidenceView(
          prev,
          next,
          pendingConfRef,
          holdRef,
          Date.now(),
        ),
      );
    };

    void pull();
    const id = window.setInterval(pull, pollMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [bufferRef, cfdOnline, sbOnline, pollMs]);

  const engines = Array.isArray(view?.engines) ? view.engines : EMPTY.engines;
  const confidenceAccounts = Array.isArray(view?.confidenceAccounts)
    ? view.confidenceAccounts
    : EMPTY.confidenceAccounts;
  const confidenceBand = view?.confidenceBand ?? "—";
  const confidencePct =
    view?.confidencePct != null && Number.isFinite(view.confidencePct)
      ? view.confidencePct
      : null;
  const preferMarket = view?.preferMarket ?? null;
  const focusMarket = view?.focusMarket ?? "—";
  const promotedMarkets = view?.promotedMarkets ?? "—";
  const rankedActive = view?.rankedActive === true;
  const marketHierarchy = view?.marketHierarchy ?? null;
  const confidenceSource = view?.confidenceSource ?? null;
  const setupMode = view?.setupMode ?? null;
  const rotationKind = view?.rotationKind ?? "off";
  const rotationLabel = view?.rotationLabel ?? "off";
  const nextBlock = view?.nextBlock ?? null;
  const preferenceReason = view?.preferenceReason ?? null;

  const primaryLine =
    confidenceBand === "—" && confidencePct == null
      ? "—"
      : [
          confidenceBand,
          confidencePct != null ? fmtPct(confidencePct) : "",
          setupMode ? `· ${setupMode}` : "",
          confidenceSource ? `· ${confidenceSource}` : "",
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
          {rankedActive ? (
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
        {engines.map((eng) => (
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

      <div className="desk-intent-shared" data-ranked={rankedActive ? "on" : "off"}>
        <div className="desk-intent-cell">
          <span className="gpu-metric-key">
            {rankedActive || preferMarket
              ? "Market focus · prefer"
              : "Market focus"}
          </span>
          <span className="desk-intent-value">
            {preferMarket && preferMarket !== "—"
              ? preferMarket
              : focusMarket}
          </span>
        </div>
        <div className="desk-intent-cell">
          <span className="gpu-metric-key">Promoted</span>
          <span
            className="desk-intent-value"
            data-promoted={rankedActive ? "on" : "off"}
            title={
              rankedActive
                ? "Ranked top-N allowlist (not DOW-only)"
                : "Ranked rotator inactive"
            }
          >
            {promotedMarkets}
          </span>
        </div>
        <div className="desk-intent-cell desk-intent-cell--confidence">
          <span className="gpu-metric-key">Confidence</span>
          {marketHierarchy ? (
            <span
              className="desk-intent-value desk-intent-hierarchy gpu-ledger-mono"
              title={preferenceReason || "Ranked market confidence"}
            >
              {marketHierarchy}
            </span>
          ) : (
            <span
              className="desk-intent-value desk-intent-conf-primary"
              data-band={confidenceBand}
              data-source={confidenceSource ?? undefined}
              title={
                confidenceSource
                  ? `Primary: ${confidenceSource} (armed / prefer account)`
                  : "No primary sniper score"
              }
            >
              {primaryLine}
            </span>
          )}
          <div className="desk-intent-conf-accounts" role="list">
            {confidenceAccounts.map((row) => (
              <span
                key={row?.id ?? "acct"}
                className="desk-intent-conf-account gpu-ledger-mono"
                role="listitem"
                data-id={row?.id}
                data-suppressed={row?.suppressed ? "1" : "0"}
                data-band={row?.band ?? "—"}
                title={
                  row?.suppressed
                    ? `${String(row?.id ?? "?").toUpperCase()} entries frozen — score suppressed`
                    : `Source: ${row?.source ?? "—"}`
                }
              >
                {row?.line ?? "—"}
              </span>
            ))}
          </div>
        </div>
        <div className="desk-intent-cell desk-intent-cell--rotation">
          <span className="gpu-metric-key">Rotation</span>
          <span
            className="desk-intent-value desk-intent-rotation-line"
            data-rot={rotationKind}
            title={rotationLabel}
          >
            {rotationLabel}
          </span>
        </div>
      </div>

      {nextBlock ? (
        <p className="desk-intent-next gpu-ledger-mono">
          Next block · {nextBlock}
        </p>
      ) : null}
    </section>
  );
}
