"use client";

import { useMemo } from "react";
import type { AlphaPoint, PositionAuthorityRow } from "@/lib/quantum-node-types";

type Props = {
  positions: PositionAuthorityRow[];
  midSeries: AlphaPoint[];
  lastMid: number;
  atrMultiple?: number;
};

const W = 640;
const H = 148;
const PAD = { t: 16, r: 12, b: 22, l: 48 };

/** Map GBP risk/profit to IG price level using £/pt size. */
function gbpToLevel(
  entry: number,
  direction: string,
  gbp: number,
  size: number,
  kind: "stop" | "profit",
): number | null {
  if (!(entry > 0) || !(size > 0) || !Number.isFinite(gbp) || gbp <= 0) {
    return null;
  }
  const pts = Math.abs(gbp) / size;
  const buy = direction.toUpperCase() !== "SELL";
  if (kind === "stop") {
    return buy ? entry - pts : entry + pts;
  }
  return buy ? entry + pts : entry - pts;
}

function pathFrom(values: Array<number | null>, yOf: (v: number) => number): string {
  const pts: string[] = [];
  let started = false;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v == null || !Number.isFinite(v)) {
      started = false;
      continue;
    }
    const x = PAD.l + (i / Math.max(1, values.length - 1)) * (W - PAD.l - PAD.r);
    const y = yOf(v);
    pts.push(`${started ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`);
    started = true;
  }
  return pts.join(" ");
}

type ChartModel = {
  dealId: string;
  label: string;
  bias: string;
  midPath: string;
  softPath: string;
  trailPath: string;
  tpPath: string;
  entryY: number | null;
  softLevel: number | null;
  trailLevel: number | null;
  tpLevel: number | null;
  softGbp: number;
  trailGbp: number | null;
  pnlGbp: number | null;
  entry: number;
};

function buildChart(
  focus: PositionAuthorityRow,
  midSeries: AlphaPoint[],
  atrMultiple: number,
): ChartModel {
  const mids = midSeries.map((p) => p.mid).filter((v) => v > 0);
  const softGbp = focus.softLossGbp != null ? Math.abs(focus.softLossGbp) : 2.2;
  const trailGbp =
    focus.trailFloorGbp != null && focus.trailFloorGbp > 0
      ? focus.trailFloorGbp
      : null;
  const targetGbp =
    focus.targetGbp != null && focus.targetGbp > 0 ? focus.targetGbp : null;

  const softLevel =
    focus.entry > 0
      ? gbpToLevel(focus.entry, focus.direction, softGbp, focus.size || 0.5, "stop")
      : null;

  const trailLevel =
    focus.entry > 0 && trailGbp != null
      ? gbpToLevel(focus.entry, focus.direction, trailGbp, focus.size || 0.5, "profit")
      : null;

  const tpFromTarget =
    focus.entry > 0 && targetGbp != null
      ? gbpToLevel(focus.entry, focus.direction, targetGbp, focus.size || 0.5, "profit")
      : null;

  const tpLevel =
    focus.takeProfit != null && focus.takeProfit > 0
      ? focus.takeProfit
      : tpFromTarget != null
        ? tpFromTarget
        : focus.entry > 0 && focus.atr > 0
          ? focus.direction === "SELL"
            ? focus.entry - atrMultiple * focus.atr
            : focus.entry + atrMultiple * focus.atr
          : null;

  const levels = [...mids, softLevel, trailLevel, tpLevel, focus.entry].filter(
    (v): v is number => v != null && v > 0,
  );

  const min = levels.length ? Math.min(...levels) : 0;
  const max = levels.length ? Math.max(...levels) : 1;
  const span = Math.max(1e-6, max - min);
  const pad = span * 0.08;
  const yMin = min - pad;
  const yMax = max + pad;
  const yOf = (v: number) =>
    PAD.t + (1 - (v - yMin) / (yMax - yMin)) * (H - PAD.t - PAD.b);

  const n = Math.max(2, midSeries.length || 2);
  const midPath = pathFrom(
    midSeries.map((p) => (p.mid > 0 ? p.mid : null)),
    yOf,
  );
  const softPath = softLevel
    ? pathFrom(Array.from({ length: n }, () => softLevel), yOf)
    : "";
  const trailPath = trailLevel
    ? pathFrom(Array.from({ length: n }, () => trailLevel), yOf)
    : "";
  const tpPath = tpLevel
    ? pathFrom(Array.from({ length: n }, () => tpLevel), yOf)
    : "";

  return {
    dealId: focus.dealId,
    label: focus.label,
    bias: focus.bias,
    midPath,
    softPath,
    trailPath,
    tpPath,
    entryY: focus.entry > 0 ? yOf(focus.entry) : null,
    softLevel,
    trailLevel,
    tpLevel,
    softGbp,
    trailGbp,
    pnlGbp: focus.pnlGbp,
    entry: focus.entry,
  };
}

function MicroChart({
  chart,
  atrMultiple,
  gradId,
}: {
  chart: ChartModel;
  atrMultiple: number;
  gradId: string;
}) {
  return (
    <article className="gpu-dyn-card" aria-label={`Dynamics ${chart.dealId}`}>
      <div className="gpu-dyn-card-head">
        <span className="gpu-chip gpu-chip--compact">
          {chart.label} · {chart.bias}
        </span>
        <span className="gpu-chip gpu-chip--compact">
          {chart.pnlGbp != null
            ? `${chart.pnlGbp >= 0 ? "+" : ""}£${chart.pnlGbp.toFixed(2)}`
            : "UPL —"}
        </span>
        <span className="gpu-chip gpu-chip--compact">{atrMultiple.toFixed(1)}× ATR</span>
      </div>
      <div className="gpu-tensor-stats gpu-tensor-stats--compact">
        <div>
          <span className="gpu-metric-key">SOFT</span>
          <strong className="gpu-stat-num gpu-tone-amber">
            {chart.softLevel != null
              ? `${chart.softLevel.toFixed(1)} (−£${chart.softGbp.toFixed(2)})`
              : "—"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">TRAIL</span>
          <strong className="gpu-stat-num gpu-metric-val--emerald">
            {chart.trailLevel != null
              ? `${chart.trailLevel.toFixed(1)} (£${(chart.trailGbp ?? 0).toFixed(2)})`
              : "UNARMED"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">TP</span>
          <strong className="gpu-stat-num">
            {chart.tpLevel != null ? chart.tpLevel.toFixed(1) : "—"}
          </strong>
        </div>
      </div>
      <div className="gpu-tensor-svg-wrap">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="gpu-tensor-svg"
          role="img"
          aria-label={`${chart.label} mid, trail, soft loss, take-profit`}
        >
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="rgba(125,211,252,0.4)" />
              <stop offset="100%" stopColor="rgba(125,211,252,1)" />
            </linearGradient>
          </defs>

          {[0.33, 0.66].map((g) => {
            const y = PAD.t + g * (H - PAD.t - PAD.b);
            return (
              <line
                key={g}
                x1={PAD.l}
                x2={W - PAD.r}
                y1={y}
                y2={y}
                stroke="rgba(148,163,184,0.1)"
              />
            );
          })}

          {chart.entryY != null ? (
            <line
              x1={PAD.l}
              x2={W - PAD.r}
              y1={chart.entryY}
              y2={chart.entryY}
              stroke="rgba(148,163,184,0.35)"
              strokeDasharray="3 4"
            />
          ) : null}

          <path
            d={chart.tpPath}
            fill="none"
            stroke="var(--gpu-emerald)"
            strokeWidth="1.5"
            strokeDasharray="6 3"
          />
          {chart.trailPath ? (
            <path
              d={chart.trailPath}
              fill="none"
              stroke="var(--gpu-amber)"
              strokeWidth="1.7"
            />
          ) : null}
          <path
            d={chart.softPath}
            fill="none"
            stroke="var(--gpu-danger)"
            strokeWidth="1.4"
            strokeDasharray="4 3"
          />
          <path
            d={chart.midPath}
            fill="none"
            stroke={`url(#${gradId})`}
            strokeWidth="2.2"
            strokeLinejoin="round"
            strokeLinecap="round"
          />

          <text x={PAD.l} y={12} className="gpu-svg-legend" fill="rgba(125,211,252,0.95)">
            mid
          </text>
          <text x={PAD.l + 36} y={12} className="gpu-svg-legend" fill="var(--gpu-amber)">
            trail_floor
          </text>
          <text x={PAD.l + 108} y={12} className="gpu-svg-legend" fill="var(--gpu-danger)">
            soft_loss
          </text>
          <text x={PAD.l + 172} y={12} className="gpu-svg-legend" fill="var(--gpu-emerald)">
            tp
          </text>
          <text
            x={PAD.l}
            y={H - 6}
            className="gpu-svg-legend"
            fill="rgba(127,147,163,0.85)"
          >
            {chart.dealId.slice(0, 14)} · entry{" "}
            {chart.entry > 0 ? chart.entry.toFixed(1) : "—"}
          </text>
        </svg>
      </div>
    </article>
  );
}

export function PositionDynamicsPanel({
  positions,
  midSeries,
  lastMid,
  atrMultiple = 3.5,
}: Props) {
  const charts = useMemo(
    () =>
      positions
        .slice(0, 6)
        .map((p) => buildChart(p, midSeries, atrMultiple)),
    [positions, midSeries, atrMultiple],
  );

  return (
    <section className="gpu-tensor-panel" aria-label="Live trailing dynamics">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Live Trailing Dynamics</p>
          <h2 className="gpu-panel-title">Position Dynamics Tracker</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip">LIVE MID {lastMid > 0 ? lastMid.toFixed(1) : "—"}</span>
          <span className="gpu-chip">{positions.length} OPEN</span>
          <span className="gpu-chip">{atrMultiple.toFixed(1)}× ATR TP</span>
        </div>
      </header>

      {charts.length === 0 ? (
        <p className="gpu-ledger-empty">NO OPEN AUTHORITY — AWAITING LIVE FILL</p>
      ) : (
        <div className="gpu-dyn-charts">
          {charts.map((c, i) => (
            <MicroChart
              key={c.dealId}
              chart={c}
              atrMultiple={atrMultiple}
              gradId={`dynMid-${i}`}
            />
          ))}
        </div>
      )}
    </section>
  );
}
