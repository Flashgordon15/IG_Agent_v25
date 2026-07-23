"use client";

import type { AssetMarketNode } from "@/lib/quantum-node-types";

type Props = {
  nodes: AssetMarketNode[];
};

function Sparkline({ ticks, tone }: { ticks: number[]; tone: string }) {
  const w = 280;
  const h = 44;
  const pad = 3;
  if (ticks.length < 2) {
    return (
      <svg className="gpu-util-canvas" viewBox={`0 0 ${w} ${h}`} aria-hidden>
        <rect width={w} height={h} fill="rgba(8,14,18,0.85)" />
        <text x={10} y={26} fill="rgba(127,147,163,0.7)" fontSize="10" fontFamily="monospace">
          AWAITING TICKS
        </text>
      </svg>
    );
  }
  const min = Math.min(...ticks);
  const max = Math.max(...ticks);
  const span = Math.max(1e-9, max - min);
  const pts = ticks
    .map((v, i) => {
      const x = pad + (i / (ticks.length - 1)) * (w - pad * 2);
      const y = pad + (1 - (v - min) / span) * (h - pad * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  const last = ticks[ticks.length - 1];
  const first = ticks[0];
  const up = last >= first;
  const stroke = tone === "bad" ? "var(--gpu-danger)" : up ? "var(--gpu-emerald)" : "var(--gpu-amber)";

  return (
    <svg
      className="gpu-util-canvas"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="0.0ms WebSocket tick sparkline"
    >
      <rect width={w} height={h} fill="rgba(8,14,18,0.85)" />
      {[0.25, 0.5, 0.75].map((g) => (
        <line
          key={g}
          x1={0}
          x2={w}
          y1={h * g}
          y2={h * g}
          stroke="rgba(52,211,153,0.08)"
          strokeWidth="1"
        />
      ))}
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth="1.75"
        strokeLinejoin="round"
        strokeLinecap="round"
        points={pts}
      />
      <circle
        cx={pad + ((ticks.length - 1) / (ticks.length - 1)) * (w - pad * 2)}
        cy={pad + (1 - (last - min) / span) * (h - pad * 2)}
        r="2.2"
        fill={stroke}
      />
    </svg>
  );
}

function toneClass(t: AssetMarketNode["secondaryTone"]): string {
  if (t === "ok") return "gpu-metric-val--emerald";
  if (t === "warn") return "gpu-tone-amber";
  if (t === "bad") return "gpu-tone-bad";
  return "";
}

export function LiveFleetPanel({ nodes }: Props) {
  return (
    <section className="gpu-fleet-panel" aria-label="Asset market data nodes">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Asset Telemetry Inversion</p>
          <h2 className="gpu-panel-title">Market Data Nodes</h2>
        </div>
        <span className="gpu-chip gpu-chip--live">
          {nodes.length} NODES · 0.0ms WS TICKS
        </span>
      </header>

      <div className="gpu-fleet-grid">
        {nodes.map((node) => {
          const hot = node.secondaryTone === "bad" || node.chipTone === "bad";
          return (
            <article
              key={node.id}
              className={`gpu-card${hot ? " gpu-card--thermal" : ""}`}
            >
              <div className="gpu-card-top">
                <div>
                  <p className="gpu-metric-key">SLOT {node.slot}</p>
                  <h3 className="gpu-card-label">{node.label}</h3>
                </div>
                <span
                  className={`gpu-chip gpu-chip--compact gpu-chip--${node.chipTone}`}
                >
                  {node.chip}
                </span>
              </div>

              <Sparkline ticks={node.ticks} tone={node.secondaryTone} />

              <div className="gpu-metrics">
                <div className="gpu-metric">
                  <span className="gpu-metric-key">{node.primaryLabel}</span>
                  <span className="gpu-metric-val gpu-metric-val--emerald">
                    {node.primaryValue}
                  </span>
                </div>
                <div className="gpu-metric">
                  <span className="gpu-metric-key">{node.secondaryLabel}</span>
                  <span className={`gpu-metric-val ${toneClass(node.secondaryTone)}`}>
                    {node.secondaryValue}
                  </span>
                </div>
                <div className="gpu-metric">
                  <span className="gpu-metric-key">MID</span>
                  <span className="gpu-metric-val">
                    {node.mid > 0
                      ? node.id === "vix"
                        ? node.mid.toFixed(1)
                        : node.mid.toFixed(node.epic.includes("EURUSD") ? 5 : 2)
                      : "—"}
                  </span>
                </div>
                <div className="gpu-metric">
                  <span className="gpu-metric-key">TICK LAT</span>
                  <span className="gpu-metric-val">
                    {node.tickLatencyMs.toFixed(2)}ms · {node.source}
                  </span>
                </div>
              </div>

              <div className="gpu-vram-track" aria-hidden>
                <div
                  className="gpu-vram-fill"
                  style={{
                    width: `${Math.min(100, 35 + (node.ticks.length / 72) * 65)}%`,
                    background:
                      node.secondaryTone === "bad"
                        ? "linear-gradient(90deg, var(--gpu-amber-soft), var(--gpu-danger))"
                        : "linear-gradient(90deg, rgba(52,211,153,0.35), var(--gpu-emerald))",
                  }}
                />
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
