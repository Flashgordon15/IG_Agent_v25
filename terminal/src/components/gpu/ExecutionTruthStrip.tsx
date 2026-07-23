"use client";

/**
 * Absolute strategy execution truth — quote age, sniper arm, active gate verdict.
 * No conviction proxies. Values paint via element refs from the multiplex buffer.
 */

import { useEffect, useRef, type MutableRefObject } from "react";
import type { GpuExecutionChrome } from "@/hooks/useGpuExecutionStream";
import type { GpuExecutionBuffer } from "@/lib/gpu-execution-buffer";
import { resolveQuoteBudgetMs } from "@/lib/desk-multiplex";

type Props = {
  bufferRef: MutableRefObject<GpuExecutionBuffer>;
  chrome: GpuExecutionChrome;
};

export function ExecutionTruthStrip({ bufferRef, chrome }: Props) {
  const ageRef = useRef<HTMLSpanElement | null>(null);
  const armRef = useRef<HTMLSpanElement | null>(null);
  const gateRef = useRef<HTMLSpanElement | null>(null);
  const rafRef = useRef(0);

  useEffect(() => {
    let alive = true;
    const loop = () => {
      if (!alive) return;
      const t = bufferRef.current.truth;
      const transport = bufferRef.current.feedTransport;
      const ceilingMs = resolveQuoteBudgetMs(transport);
      const ageEl = ageRef.current;
      if (ageEl) {
        const ms = t.quoteAgeMs;
        if (ms == null || !Number.isFinite(ms)) {
          ageEl.textContent = "—";
          ageEl.dataset.tone = "mute";
        } else {
          ageEl.textContent = `${Math.round(ms)} ms`;
          ageEl.dataset.tone = ms > ceilingMs ? "crimson" : "live";
        }
      }
      if (armRef.current) {
        armRef.current.textContent = t.sniperArm;
        armRef.current.dataset.arm = t.sniperArm.toLowerCase();
      }
      if (gateRef.current) {
        gateRef.current.textContent = t.gateVerdict || "GATE_UNKNOWN";
      }
      rafRef.current = requestAnimationFrame(loop);
    };
    rafRef.current = requestAnimationFrame(loop);
    return () => {
      alive = false;
      cancelAnimationFrame(rafRef.current);
    };
  }, [bufferRef]);

  return (
    <section className="exec-truth-strip" aria-label="Execution truth strip">
      <div className="exec-truth-head">
        <p className="gpu-kicker">Absolute Strategy Execution Truth</p>
        <span className="gpu-chip gpu-chip--mono">
          MUX {chrome.muxSource.toUpperCase()} · WS {chrome.wsState.toUpperCase()}
        </span>
      </div>
      <div className="exec-truth-grid">
        <div className="exec-truth-cell">
          <span className="gpu-metric-key">LIVE QUOTE AGE</span>
          <span ref={ageRef} className="exec-truth-value exec-truth-age">
            —
          </span>
          <span className="exec-truth-sub">
            adaptive ceiling · crimson = fail-closed
          </span>
        </div>
        <div className="exec-truth-cell">
          <span className="gpu-metric-key">SNIPER ARMING STATE</span>
          <span ref={armRef} className="exec-truth-value exec-truth-arm">
            {chrome.sniperArm}
          </span>
          <span className="exec-truth-sub">Active · Suppressed · Vetoed</span>
        </div>
        <div className="exec-truth-cell exec-truth-cell--wide">
          <span className="gpu-metric-key">ACTIVE GATE VERDICT</span>
          <span ref={gateRef} className="exec-truth-value exec-truth-gate">
            {chrome.gateVerdict}
          </span>
          <span className="exec-truth-sub">
            raw agent gate / wait / veto — no UI proxies
          </span>
        </div>
      </div>
    </section>
  );
}
