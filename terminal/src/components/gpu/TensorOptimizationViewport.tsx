"use client";

import { useMemo } from "react";
import type { AlphaPoint } from "@/lib/quantum-node-types";

type Props = {
  epic: string;
  label: string;
  series: AlphaPoint[];
  lastMid: number;
  lastObi: number;
};

const W = 640;
const H = 220;
const PAD = { t: 16, r: 14, b: 48, l: 48 };
const OBI_BAND = 0.15;

export function TensorOptimizationViewport({
  epic,
  label,
  series,
  lastMid,
  lastObi,
}: Props) {
  const chart = useMemo(() => {
    const mids = series.map((p) => p.mid).filter((v) => v > 0);
    const minMid = mids.length ? Math.min(...mids) : 0;
    const maxMid = mids.length ? Math.max(...mids) : 1;
    const span = Math.max(1e-6, maxMid - minMid);
    const innerW = W - PAD.l - PAD.r;
    const priceH = (H - PAD.t - PAD.b) * 0.62;
    const histTop = PAD.t + priceH + 10;
    const histH = H - PAD.b - histTop;

    let midPath = "";
    series.forEach((p, i) => {
      if (!(p.mid > 0) || series.length < 2) return;
      const x = PAD.l + (i / (series.length - 1)) * innerW;
      const y = PAD.t + (1 - (p.mid - minMid) / span) * priceH;
      midPath += `${i === 0 || !midPath ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)} `;
    });

    const bars = series.map((p, i) => {
      const x = PAD.l + (i / Math.max(1, series.length - 1)) * innerW;
      const mag = Math.abs(p.obi) / OBI_BAND;
      const bh = Math.max(1, mag * histH * 0.92);
      const y = p.obi >= 0 ? histTop + histH / 2 - bh : histTop + histH / 2;
      return {
        x,
        y,
        h: bh,
        w: Math.max(2, innerW / Math.max(series.length, 1) - 1.2),
        pos: p.obi >= 0,
      };
    });

    return { midPath, bars, histTop, histH, minMid, maxMid };
  }, [series]);

  return (
    <section className="gpu-tensor-panel" aria-label="High-velocity alpha visualizer">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">High-Velocity Alpha Visualizer</p>
          <h2 className="gpu-panel-title">Mid-Drift × OBI Velocity</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip">{label}</span>
          <span className="gpu-chip">OBI ±{OBI_BAND.toFixed(2)}</span>
        </div>
      </header>

      <div className="gpu-tensor-stats">
        <div>
          <span className="gpu-metric-key">FOCUS EPIC</span>
          <strong className="gpu-stat-num" style={{ fontSize: "0.85rem" }}>
            {epic.split(".").slice(0, 3).join(".")}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">MID</span>
          <strong className="gpu-stat-num">
            {lastMid > 0 ? lastMid.toFixed(1) : "—"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">OBI NOW</span>
          <strong
            className="gpu-stat-num"
            style={{
              color:
                Math.abs(lastObi) > 0.1
                  ? "var(--gpu-amber)"
                  : "var(--gpu-emerald)",
            }}
          >
            {lastObi >= 0 ? "+" : ""}
            {lastObi.toFixed(3)}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">SAMPLES</span>
          <strong className="gpu-stat-num">{series.length}</strong>
        </div>
      </div>

      <div className="gpu-tensor-svg-wrap">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="gpu-tensor-svg"
          role="img"
          aria-label="Mid price drift and order book imbalance histogram"
        >
          <defs>
            <linearGradient id="midGrad" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="rgba(52,211,153,0.35)" />
              <stop offset="100%" stopColor="rgba(52,211,153,1)" />
            </linearGradient>
          </defs>

          {[0.25, 0.5, 0.75].map((g) => {
            const y = PAD.t + g * (H - PAD.t - PAD.b) * 0.62;
            return (
              <line
                key={g}
                x1={PAD.l}
                x2={W - PAD.r}
                y1={y}
                y2={y}
                stroke="rgba(148,163,184,0.12)"
              />
            );
          })}

          <path
            d={chart.midPath}
            fill="none"
            stroke="url(#midGrad)"
            strokeWidth="2.2"
            strokeLinejoin="round"
            strokeLinecap="round"
          />

          {/* OBI zero line */}
          <line
            x1={PAD.l}
            x2={W - PAD.r}
            y1={chart.histTop + chart.histH / 2}
            y2={chart.histTop + chart.histH / 2}
            stroke="rgba(251,191,36,0.35)"
            strokeDasharray="4 3"
          />

          {chart.bars.map((b, i) => (
            <rect
              key={i}
              x={b.x - b.w / 2}
              y={b.y}
              width={b.w}
              height={b.h}
              fill={
                b.pos ? "rgba(52,211,153,0.75)" : "rgba(251,191,36,0.8)"
              }
              opacity={0.85}
            />
          ))}

          <text x={PAD.l} y={14} className="gpu-svg-legend" fill="rgba(52,211,153,0.95)">
            mid-drift
          </text>
          <text x={PAD.l + 78} y={14} className="gpu-svg-legend" fill="rgba(251,191,36,0.95)">
            obi histogram ±0.15
          </text>
          <text
            x={PAD.l}
            y={H - 10}
            className="gpu-svg-legend"
            fill="rgba(127,147,163,0.85)"
          >
            {chart.minMid > 0
              ? `${chart.minMid.toFixed(1)} → ${chart.maxMid.toFixed(1)}`
              : "warming mid band"}
          </text>
        </svg>
      </div>
    </section>
  );
}
