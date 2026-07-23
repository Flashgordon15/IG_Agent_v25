"use client";

/**
 * v36 tactical radar — compact OBI + Grok macro bias readout (ref-painted).
 */

import { useEffect, useRef } from "react";

type Props = {
  macroBias: string;
  lastObi: number;
  focusLabel: string;
};

const OBI_BAND = 0.15;

export function TacticalMacroObiStrip({ macroBias, lastObi, focusLabel }: Props) {
  const biasRef = useRef<HTMLSpanElement | null>(null);
  const obiRef = useRef<HTMLSpanElement | null>(null);
  const barRef = useRef<HTMLDivElement | null>(null);
  const focusRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    const bias = String(macroBias || "NEUTRAL").toUpperCase();
    if (biasRef.current) {
      biasRef.current.textContent = bias;
      biasRef.current.dataset.tone =
        bias === "VETO" || bias === "RISK_OFF"
          ? "bad"
          : bias === "BULL" || bias === "RISK_ON"
            ? "live"
            : "mute";
    }
    const obi = Number.isFinite(lastObi) ? lastObi : 0;
    if (obiRef.current) {
      obiRef.current.textContent = `${obi >= 0 ? "+" : ""}${obi.toFixed(3)}`;
      obiRef.current.dataset.tone =
        Math.abs(obi) >= OBI_BAND * 0.85 ? "warn" : "live";
    }
    if (barRef.current) {
      const pct = Math.min(100, (Math.abs(obi) / OBI_BAND) * 100);
      barRef.current.style.width = `${pct}%`;
      barRef.current.dataset.side = obi >= 0 ? "bid" : "ask";
    }
    if (focusRef.current) {
      focusRef.current.textContent = focusLabel || "DOW";
    }
  }, [macroBias, lastObi, focusLabel]);

  return (
    <section className="v36-macro-obi-strip" aria-label="OBI and macro bias">
      <div className="v36-macro-obi-cell">
        <span className="gpu-kicker">Grok Macro Bias</span>
        <strong
          ref={biasRef}
          className="v36-macro-value gpu-ledger-mono"
          data-tone="mute"
        >
          NEUTRAL
        </strong>
      </div>
      <div className="v36-macro-obi-cell v36-macro-obi-cell--grow">
        <span className="gpu-kicker">
          OBI <span ref={focusRef}>DOW</span> ±{OBI_BAND.toFixed(2)}
        </span>
        <div className="v36-obi-row">
          <strong ref={obiRef} className="v36-obi-value gpu-ledger-mono" data-tone="live">
            +0.000
          </strong>
          <div className="v36-obi-track" aria-hidden>
            <div ref={barRef} className="v36-obi-fill" data-side="bid" />
          </div>
        </div>
      </div>
    </section>
  );
}
