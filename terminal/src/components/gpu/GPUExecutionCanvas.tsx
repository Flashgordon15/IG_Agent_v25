"use client";

/**
 * GPU-Accelerated HTML5 Risk Canvas — hardware RAF loop.
 * Reads mutable GpuExecutionBuffer directly; no per-tick React re-renders.
 */

import { useEffect, useRef, type MutableRefObject } from "react";
import type { GpuExecutionChrome } from "@/hooks/useGpuExecutionStream";
import {
  gbpToPriceLevel,
  type GpuExecutionBuffer,
  type GpuExecPosition,
} from "@/lib/gpu-execution-buffer";

type Props = {
  bufferRef: MutableRefObject<GpuExecutionBuffer>;
  chrome: GpuExecutionChrome;
  milestoneGbp?: number;
  progressPct?: number;
  provisionalCashGbp?: number;
};

const FONT = '11px "IBM Plex Mono", ui-monospace, Menlo, monospace';
const FONT_SM = '9px "IBM Plex Mono", ui-monospace, Menlo, monospace';
const STANDBY_BG = "#111111";

function bothAccountsFlat(buf: GpuExecutionBuffer): boolean {
  const cfd = buf.engines.find((l) => l.engineId === "cfd_sniper");
  const sb = buf.engines.find((l) => l.engineId === "sb_sentinel");
  const cfdOpen = cfd?.openCount ?? 0;
  const sbOpen = sb?.openCount ?? 0;
  return cfdOpen === 0 && sbOpen === 0 && buf.positions.length === 0;
}

function drawStandby(ctx: CanvasRenderingContext2D, cssW: number, cssH: number): void {
  const dpr = Math.min(2.5, window.devicePixelRatio || 1);
  const canvas = ctx.canvas;
  const needW = Math.floor(cssW * dpr);
  const needH = Math.floor(cssH * dpr);
  if (canvas.width !== needW || canvas.height !== needH) {
    canvas.width = needW;
    canvas.height = needH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = STANDBY_BG;
  ctx.fillRect(0, 0, cssW, cssH);

  const step = 32;
  ctx.strokeStyle = "rgba(255, 255, 255, 0.035)";
  ctx.lineWidth = 1;
  for (let x = 0; x <= cssW; x += step) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, cssH);
    ctx.stroke();
  }
  for (let y = 0; y <= cssH; y += step) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(cssW, y);
    ctx.stroke();
  }

  ctx.font = FONT;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "rgba(148, 163, 184, 0.42)";
  ctx.fillText(
    "[📉 NO OPEN TRACKING PATHS - GPU STANDBY]",
    cssW / 2,
    cssH / 2,
  );
  ctx.textAlign = "start";
  ctx.textBaseline = "alphabetic";
}

function drawChannel(
  ctx: CanvasRenderingContext2D,
  cssW: number,
  cssH: number,
  buf: GpuExecutionBuffer,
) {
  const dpr = Math.min(2.5, window.devicePixelRatio || 1);
  const canvas = ctx.canvas;
  const needW = Math.floor(cssW * dpr);
  const needH = Math.floor(cssH * dpr);
  if (canvas.width !== needW || canvas.height !== needH) {
    canvas.width = needW;
    canvas.height = needH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  // Premium dark plane
  const bg = ctx.createLinearGradient(0, 0, 0, cssH);
  bg.addColorStop(0, "rgba(10, 16, 22, 0.98)");
  bg.addColorStop(1, "rgba(6, 10, 14, 0.98)");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, cssW, cssH);

  const pad = { t: 28, r: 14, b: 36, l: 52 };
  const focus: GpuExecPosition | undefined =
    buf.positions.find((p) => p.epic === buf.focusEpic) ?? buf.positions[0];
  const mids = buf.mids;
  const entry = focus?.entry ?? 0;

  const softGbp =
    focus?.softLossGbp != null && focus.softLossGbp > 0
      ? Math.abs(focus.softLossGbp)
      : null;
  const trailGbp =
    focus?.trailFloorGbp != null && focus.trailFloorGbp > 0
      ? focus.trailFloorGbp
      : null;
  const softLevel =
    focus && softGbp
      ? gbpToPriceLevel(
          focus.entry,
          focus.direction,
          softGbp,
          focus.size,
          "stop",
          focus.epic,
        )
      : null;
  const trailLevel =
    focus && trailGbp
      ? gbpToPriceLevel(
          focus.entry,
          focus.direction,
          trailGbp,
          focus.size,
          "profit",
          focus.epic,
        )
      : null;

  const levels = [
    ...mids,
    ...buf.trailCrawl,
    ...buf.softCrawl,
    entry,
    softLevel,
    trailLevel,
    buf.lastMid,
  ].filter((v): v is number => v != null && v > 0);

  // Header chrome drawn on canvas (no DOM text churn)
  ctx.font = FONT_SM;
  ctx.fillStyle = "rgba(148, 163, 184, 0.55)";
  ctx.fillText("GPU EXECUTION CHANNEL · PRICE × SOFTWARE TRAIL", pad.l, 14);
  ctx.fillStyle = buf.wsLive
    ? "rgba(52, 211, 153, 0.95)"
    : "rgba(148, 163, 184, 0.45)";
  ctx.fillText(buf.wsLive ? "WS LIVE" : "WS FALLBACK", cssW - pad.r - 72, 14);

  if (!levels.length) {
    ctx.fillStyle = "rgba(148, 163, 184, 0.55)";
    ctx.font = FONT;
    ctx.fillText("AWAITING LIVE TICK STREAM…", pad.l, cssH / 2);
    return;
  }

  const min = Math.min(...levels);
  const max = Math.max(...levels);
  const span = Math.max(1e-6, max - min) * 1.18;
  const midSpan = (max + min) / 2;
  const yMin = midSpan - span / 2;
  const yMax = midSpan + span / 2;
  const yOf = (v: number) =>
    pad.t + (1 - (v - yMin) / (yMax - yMin)) * (cssH - pad.t - pad.b);
  const xOf = (i: number, n: number) =>
    pad.l + (i / Math.max(1, n - 1)) * (cssW - pad.l - pad.r);

  // Grid
  ctx.strokeStyle = "rgba(148, 163, 184, 0.08)";
  ctx.lineWidth = 1;
  for (const g of [0.25, 0.5, 0.75]) {
    const y = pad.t + g * (cssH - pad.t - pad.b);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(cssW - pad.r, y);
    ctx.stroke();
  }

  // Price channel fill under mid path
  if (mids.length >= 2) {
    ctx.beginPath();
    mids.forEach((v, i) => {
      const x = xOf(i, mids.length);
      const y = yOf(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    const lastX = xOf(mids.length - 1, mids.length);
    const firstX = xOf(0, mids.length);
    ctx.lineTo(lastX, cssH - pad.b);
    ctx.lineTo(firstX, cssH - pad.b);
    ctx.closePath();
    const fill = ctx.createLinearGradient(0, pad.t, 0, cssH - pad.b);
    fill.addColorStop(0, "rgba(125, 211, 252, 0.14)");
    fill.addColorStop(1, "rgba(125, 211, 252, 0.01)");
    ctx.fillStyle = fill;
    ctx.fill();

    const grad = ctx.createLinearGradient(pad.l, 0, cssW - pad.r, 0);
    grad.addColorStop(0, "rgba(125, 211, 252, 0.35)");
    grad.addColorStop(1, "rgba(125, 211, 252, 1)");
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

  const rail = (
    level: number | null,
    color: string,
    dash: number[],
    width = 1.6,
  ) => {
    if (level == null) return;
    const y = yOf(level);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(cssW - pad.r, y);
    ctx.stroke();
    ctx.setLineDash([]);
  };

  // Entry — distinct dotted line
  if (entry > 0) {
    rail(entry, "rgba(226, 232, 240, 0.55)", [2, 5], 1.8);
    ctx.fillStyle = "rgba(226, 232, 240, 0.75)";
    ctx.font = FONT_SM;
    ctx.fillText(`ENTRY ${entry.toFixed(1)}`, pad.l + 4, yOf(entry) - 4);
  }

  // Soft loss — static rail + crawl history
  if (buf.softCrawl.length >= 2) {
    ctx.strokeStyle = "rgba(255, 77, 109, 0.55)";
    ctx.lineWidth = 1.4;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    buf.softCrawl.forEach((v, i) => {
      const x = xOf(i, buf.softCrawl.length);
      const y = yOf(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
  }
  rail(softLevel, "rgba(255, 77, 109, 0.95)", [5, 4], 1.7);

  // Trail floor — crawling indicator climbing with profit
  if (buf.trailCrawl.length >= 2) {
    ctx.strokeStyle = "rgba(251, 191, 36, 0.7)";
    ctx.lineWidth = 2;
    ctx.setLineDash([]);
    ctx.beginPath();
    buf.trailCrawl.forEach((v, i) => {
      const x = xOf(i, buf.trailCrawl.length);
      const y = yOf(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // Head marker
    const hx = xOf(buf.trailCrawl.length - 1, buf.trailCrawl.length);
    const hy = yOf(buf.trailCrawl[buf.trailCrawl.length - 1]!);
    ctx.fillStyle = "rgba(251, 191, 36, 1)";
    ctx.beginPath();
    ctx.arc(hx, hy, 3.5, 0, Math.PI * 2);
    ctx.fill();
  }
  rail(trailLevel, "rgba(251, 191, 36, 0.95)", [], 1.5);

  // Y labels
  ctx.font = FONT_SM;
  ctx.fillStyle = "rgba(148, 163, 184, 0.55)";
  ctx.fillText(yMax.toFixed(1), 4, pad.t + 8);
  ctx.fillText(yMin.toFixed(1), 4, cssH - pad.b);

  // Legend — reserved bottom pad so labels never sit on the price path
  const legendY = cssH - 12;
  ctx.fillStyle = "rgba(6, 10, 14, 0.82)";
  ctx.fillRect(0, cssH - 26, cssW, 26);
  ctx.font = FONT_SM;
  ctx.fillStyle = "rgba(125, 211, 252, 0.9)";
  ctx.fillText("TICK", pad.l, legendY);
  ctx.fillStyle = "rgba(226, 232, 240, 0.7)";
  ctx.fillText("ENTRY", pad.l + 40, legendY);
  ctx.fillStyle = "rgba(255, 77, 109, 0.9)";
  ctx.fillText("SOFT", pad.l + 90, legendY);
  ctx.fillStyle = "rgba(251, 191, 36, 0.95)";
  ctx.fillText("TRAIL", pad.l + 140, legendY);

  if (focus) {
    const pnl = focus.pnlGbp;
    ctx.fillStyle =
      pnl == null
        ? "rgba(148,163,184,0.6)"
        : pnl >= 0
          ? "rgba(52,211,153,0.95)"
          : "rgba(255,77,109,0.95)";
    const pnlTxt =
      pnl == null
        ? "UPL —"
        : `UPL ${pnl >= 0 ? "+" : "−"}£${Math.abs(pnl).toFixed(2)}`;
    ctx.fillText(
      `${focus.label} ${focus.direction} · ${pnlTxt}`,
      cssW - pad.r - 200,
      legendY,
    );
  }

  // Multi-position entry ticks on right margin
  if (buf.positions.length > 1) {
    let yi = pad.t + 4;
    ctx.font = FONT_SM;
    for (const p of buf.positions.slice(0, 6)) {
      ctx.fillStyle = "rgba(148, 163, 184, 0.5)";
      ctx.fillText(
        `${p.label.slice(0, 8)} @ ${p.entry > 0 ? p.entry.toFixed(1) : "—"}`,
        cssW - pad.r - 110,
        yi,
      );
      yi += 12;
    }
  }
}

export function GPUExecutionCanvas({
  bufferRef,
  chrome,
  milestoneGbp = 1000,
  progressPct = 0,
  provisionalCashGbp = 0,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef(0);
  const progress = Math.min(100, Math.max(0, progressPct));
  const muxUpl = chrome.sessionUnrealizedGbp;
  const muxRealized = chrome.sessionRealizedGbp;

  useEffect(() => {
    let alive = true;
    const loop = () => {
      if (!alive) return;
      const canvas = canvasRef.current;
      const wrap = wrapRef.current;
      if (canvas && wrap) {
        const ctx = canvas.getContext("2d", { alpha: false });
        if (ctx) {
          const rect = wrap.getBoundingClientRect();
          const w = Math.max(320, Math.floor(rect.width));
          const h = Math.max(280, Math.floor(rect.height));
          const buf = bufferRef.current;
          if (bothAccountsFlat(buf)) {
            drawStandby(ctx, w, h);
          } else {
            drawChannel(ctx, w, h, buf);
          }
        }
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
    <section
      className="gpu-tensor-panel gpu-exec-panel"
      aria-label="GPU execution risk canvas"
    >
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">GPU Chart Viewport</p>
          <h2 className="gpu-panel-title">Price Channel · Entry · Trail</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span
            className={`gpu-chip ${chrome.wsState === "live" ? "gpu-chip--live" : ""}`}
          >
            WS {chrome.wsState.toUpperCase()}
          </span>
          <span className="gpu-chip">
            {chrome.focusLabel} · OPEN {chrome.openCount}
          </span>
          <span className="gpu-chip gpu-chip--mono">
            MID{" "}
            {chrome.lastMid > 0 ? chrome.lastMid.toFixed(1) : "—"}
          </span>
          <span className="gpu-chip gpu-chip--mono">
            MUX UPL {muxUpl >= 0 ? "+" : "−"}£{Math.abs(muxUpl).toFixed(2)}
          </span>
        </div>
      </header>
      <div className="sniper-milestone-strip gpu-exec-milestone" aria-label="Daily milestone">
        <div className="sniper-milestone-meta">
          <span className="gpu-ledger-mono">
            £{(provisionalCashGbp || muxRealized).toFixed(2)} / £
            {milestoneGbp.toFixed(0)}
          </span>
          <span className="sniper-meta">{progress.toFixed(1)}% TO £1k</span>
        </div>
        <div className="gpu-milestone-track sniper-daily-track" aria-hidden>
          <div className="gpu-milestone-fill" style={{ width: `${progress}%` }} />
        </div>
      </div>
      <div ref={wrapRef} className="gpu-exec-canvas-wrap v36-gpu-frame">
        <canvas
          ref={canvasRef}
          className="gpu-exec-canvas"
          aria-label="Live price channel with entry and trailing stop overlays"
        />
      </div>
      <p className="gpu-exec-footnote">
        RAF · mux WS positions + trail_floor_gbp crawl · revision {chrome.revision} ·
        zero DOM tick re-renders
      </p>
    </section>
  );
}
