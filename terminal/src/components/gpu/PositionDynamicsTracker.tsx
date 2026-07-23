"use client";

import { useEffect, useMemo, useRef } from "react";
import type { AlphaPoint, PositionAuthorityRow } from "@/lib/quantum-node-types";

type Props = {
  positions: PositionAuthorityRow[];
  midSeries: AlphaPoint[];
  lastMid: number;
};

const W = 640;
const H = 112;

function gbpToLevel(
  entry: number,
  direction: string,
  gbp: number,
  size: number,
  kind: "stop" | "profit",
): number | null {
  if (!(entry > 0) || !(size > 0) || !Number.isFinite(gbp) || gbp <= 0) return null;
  const pts = Math.abs(gbp) / size;
  const buy = direction.toUpperCase() !== "SELL";
  if (kind === "stop") return buy ? entry - pts : entry + pts;
  return buy ? entry + pts : entry - pts;
}

function dirLabel(direction: string): string {
  return direction.toUpperCase() === "SELL" ? "SHORT" : "LONG";
}

function drawTracker(
  ctx: CanvasRenderingContext2D,
  mids: number[],
  entry: number,
  softLevel: number | null,
  trailLevel: number | null,
) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "rgba(8,14,18,0.9)";
  ctx.fillRect(0, 0, W, H);

  const pad = { t: 10, r: 10, b: 14, l: 44 };
  const levels = [...mids, entry, softLevel, trailLevel].filter(
    (v): v is number => v != null && v > 0,
  );
  if (!levels.length) {
    ctx.fillStyle = "rgba(127,147,163,0.7)";
    ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillText("AWAITING LIVE MID STREAM", pad.l, H / 2);
    return;
  }
  const min = Math.min(...levels);
  const max = Math.max(...levels);
  const span = Math.max(1e-6, max - min) * 1.12;
  const midSpan = (max + min) / 2;
  const yMin = midSpan - span / 2;
  const yMax = midSpan + span / 2;
  const yOf = (v: number) =>
    pad.t + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.t - pad.b);
  const xOf = (i: number, n: number) =>
    pad.l + (i / Math.max(1, n - 1)) * (W - pad.l - pad.r);

  ctx.strokeStyle = "rgba(148,163,184,0.12)";
  ctx.lineWidth = 1;
  for (const g of [0.33, 0.66]) {
    const y = pad.t + g * (H - pad.t - pad.b);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
  }

  const rail = (level: number | null, color: string, dash?: number[]) => {
    if (level == null) return;
    const y = yOf(level);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.setLineDash(dash ?? []);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.setLineDash([]);
  };

  rail(entry, "rgba(148,163,184,0.45)", [3, 4]);
  rail(softLevel, "rgba(255,77,109,0.95)", [4, 3]);
  rail(trailLevel, "rgba(251,191,36,0.95)");

  if (mids.length >= 2) {
    const grad = ctx.createLinearGradient(pad.l, 0, W - pad.r, 0);
    grad.addColorStop(0, "rgba(125,211,252,0.35)");
    grad.addColorStop(1, "rgba(125,211,252,1)");
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2.2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    mids.forEach((v, i) => {
      const x = xOf(i, mids.length);
      const y = yOf(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  ctx.fillStyle = "rgba(127,147,163,0.75)";
  ctx.font = "9px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.fillText("mid", pad.l, 9);
  ctx.fillStyle = "rgba(251,191,36,0.95)";
  ctx.fillText("trail_floor", pad.l + 28, 9);
  ctx.fillStyle = "rgba(255,77,109,0.95)";
  ctx.fillText("soft_loss", pad.l + 96, 9);
}

function TrackerCard({
  position,
  midSeries,
  lastMid,
}: {
  position: PositionAuthorityRow;
  midSeries: AlphaPoint[];
  lastMid: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const softGbp = position.softLossGbp != null ? Math.abs(position.softLossGbp) : 2.2;
  const trailGbp =
    position.trailFloorGbp != null && position.trailFloorGbp > 0
      ? position.trailFloorGbp
      : null;

  const softLevel = useMemo(
    () =>
      gbpToLevel(
        position.entry,
        position.direction,
        softGbp,
        position.size || 0.5,
        "stop",
      ),
    [position.entry, position.direction, position.size, softGbp],
  );
  const trailLevel = useMemo(
    () =>
      trailGbp != null
        ? gbpToLevel(
            position.entry,
            position.direction,
            trailGbp,
            position.size || 0.5,
            "profit",
          )
        : null,
    [position.entry, position.direction, position.size, trailGbp],
  );

  const liveMid = position.currentMid ?? lastMid;
  const mids = useMemo(() => {
    const series = midSeries.map((p) => p.mid).filter((v) => v > 0);
    if (liveMid > 0 && (series.length === 0 || series[series.length - 1] !== liveMid)) {
      return [...series, liveMid].slice(-96);
    }
    return series;
  }, [midSeries, liveMid]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = "100%";
    canvas.style.height = `${H}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    drawTracker(ctx, mids, position.entry, softLevel, trailLevel);
  }, [mids, position.entry, softLevel, trailLevel]);

  const delta =
    liveMid > 0 && position.entry > 0 ? liveMid - position.entry : null;

  return (
    <article className="trail-card" aria-label={`Trail ${position.dealId}`}>
      <header className="trail-card-head">
        <div>
          <span className="gpu-metric-key">DEAL ID</span>
          <strong className="gpu-ledger-mono">{position.dealId}</strong>
        </div>
        <div>
          <span className="gpu-metric-key">DIRECTION</span>
          <strong
            className={
              dirLabel(position.direction) === "LONG"
                ? "gpu-metric-val--emerald"
                : "sniper-flag--short-text"
            }
          >
            {dirLabel(position.direction)}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">ASSET</span>
          <strong>{position.label}</strong>
        </div>
        <div>
          <span className="gpu-metric-key">UPL</span>
          <strong
            className={
              position.pnlGbp != null && position.pnlGbp < 0
                ? "gpu-tone-loss"
                : "gpu-tone-profit"
            }
          >
            {position.pnlGbp != null
              ? `${position.pnlGbp >= 0 ? "+" : ""}£${position.pnlGbp.toFixed(2)}`
              : "—"}
          </strong>
        </div>
      </header>

      <div className="trail-streams">
        <div>
          <span className="gpu-metric-key">CURRENT MID</span>
          <strong className="trail-num">
            {liveMid > 0 ? liveMid.toFixed(1) : "—"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">TRUE ENTRY</span>
          <strong className="trail-num">
            {position.entry > 0 ? position.entry.toFixed(1) : "—"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">Δ MID−ENTRY</span>
          <strong
            className={`trail-num ${
              delta != null && delta < 0 ? "gpu-tone-loss" : "gpu-tone-profit"
            }`}
          >
            {delta != null ? `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}` : "—"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">SOFT LOSS £</span>
          <strong className="trail-num gpu-tone-amber">
            {softLevel != null
              ? `${softLevel.toFixed(1)} (−£${softGbp.toFixed(2)})`
              : "—"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">TRAIL FLOOR £</span>
          <strong className="trail-num gpu-metric-val--emerald">
            {trailLevel != null && trailGbp != null
              ? `${trailLevel.toFixed(1)} (£${trailGbp.toFixed(2)} climbing)`
              : "UNARMED"}
          </strong>
        </div>
        <div>
          <span className="gpu-metric-key">PEAK PROFIT</span>
          <strong className="trail-num">
            {position.peakProfitGbp != null
              ? `£${position.peakProfitGbp.toFixed(2)}`
              : "—"}
          </strong>
        </div>
      </div>

      <canvas
        ref={canvasRef}
        className="trail-canvas"
        role="img"
        aria-label="Live trailing protection track"
      />
    </article>
  );
}

export function PositionDynamicsTracker({
  positions,
  midSeries,
  lastMid,
}: Props) {
  return (
    <section
      className="gpu-tensor-panel trail-panel"
      aria-label="Live trailing protection track"
    >
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Open Trailing Protection</p>
          <h2 className="gpu-panel-title">Live Risk Canvas</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip">{positions.length} RUNNING</span>
          <span className="gpu-chip">SOFT + TRAIL FLOORS</span>
        </div>
      </header>

      {positions.length === 0 ? (
        <p className="gpu-ledger-empty">NO RUNNING POSITIONS — TRAIL STACK IDLE</p>
      ) : (
        <div className="trail-stack">
          {positions.slice(0, 8).map((p) => (
            <TrackerCard
              key={p.dealId}
              position={p}
              midSeries={midSeries}
              lastMid={lastMid}
            />
          ))}
        </div>
      )}
    </section>
  );
}
