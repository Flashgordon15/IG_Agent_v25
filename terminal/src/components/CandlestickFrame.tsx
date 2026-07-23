"use client";

import { candleBarsFromHistory } from "@/lib/fulfillment-parse";
import { EPIC_LABELS } from "@/lib/constants";

type Props = {
  epic: string;
  history: number[];
};

export function CandlestickFrame({ epic, history }: Props) {
  const bars = candleBarsFromHistory(history);
  const w = 960;
  const h = 420;
  const padX = 28;
  const padY = 22;
  const prices = bars.flatMap((b) => [b.h, b.l]);
  const min = Math.min(...prices, 0);
  const max = Math.max(...prices, 1);
  const range = max - min || 1;
  const slot = (w - padX * 2) / Math.max(bars.length, 1);
  const barW = Math.max(5, Math.min(18, slot * 0.62));

  const y = (p: number) => padY + (h - padY * 2) * (1 - (p - min) / range);
  const last = bars[bars.length - 1];
  const lastPx = last ? last.c : 0;
  const upSession = bars.length >= 2 ? bars[bars.length - 1].c >= bars[0].o : true;

  const gridYs = [0.2, 0.4, 0.6, 0.8].map((t) => padY + (h - padY * 2) * t);

  return (
    <div className="cq-panel relative flex h-full min-h-0 flex-1 flex-col overflow-hidden">
      <div
        className="flex items-end justify-between border-b border-[var(--color-desk-line)]"
        style={{ padding: "0.7rem 1rem" }}
      >
        <div>
          <div className="cq-label mb-1">Price</div>
          <div className="flex items-baseline gap-3">
            <h2 className="text-[length:clamp(1rem,0.9rem+0.35vw,1.35rem)] font-semibold tracking-tight">
              {EPIC_LABELS[epic] ?? epic}
            </h2>
            <span
              className={`cq-mono text-[length:clamp(1rem,0.9rem+0.3vw,1.3rem)] font-semibold ${
                upSession
                  ? "text-[var(--color-desk-good)]"
                  : "text-[var(--color-desk-bad)]"
              }`}
            >
              {lastPx ? lastPx.toFixed(lastPx > 100 ? 2 : 5) : "—"}
            </span>
          </div>
        </div>
        <span className="cq-chip">
          {bars.length} bars · live-derived
        </span>
      </div>

      <div className="relative min-h-0 flex-1 p-3">
        <svg
          viewBox={`0 0 ${w} ${h}`}
          className="h-full w-full"
          preserveAspectRatio="xMidYMid meet"
          style={{ shapeRendering: "geometricPrecision" }}
        >
          <defs>
            <linearGradient id="cqBull" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#5af0b0" />
              <stop offset="100%" stopColor="#1f9d68" />
            </linearGradient>
            <linearGradient id="cqBear" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ff7a93" />
              <stop offset="100%" stopColor="#c2304d" />
            </linearGradient>
            <linearGradient id="cqArea" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgba(91,141,239,0.18)" />
              <stop offset="100%" stopColor="rgba(91,141,239,0)" />
            </linearGradient>
          </defs>

          {gridYs.map((gy) => (
            <line
              key={gy}
              x1={padX}
              x2={w - padX}
              y1={gy}
              y2={gy}
              stroke="rgba(148,163,184,0.1)"
              strokeWidth="1"
            />
          ))}

          {bars.length > 1 ? (
            <path
              d={
                `M ${padX + slot * 0.5} ${y(bars[0].c)} ` +
                bars
                  .map((b, i) => `L ${padX + i * slot + slot * 0.5} ${y(b.c)}`)
                  .join(" ") +
                ` L ${padX + (bars.length - 1) * slot + slot * 0.5} ${h - padY}` +
                ` L ${padX + slot * 0.5} ${h - padY} Z`
              }
              fill="url(#cqArea)"
            />
          ) : null}

          {bars.map((bar, i) => {
            const x = padX + i * slot + (slot - barW) / 2;
            const bull = bar.c >= bar.o;
            const color = bull ? "url(#cqBull)" : "url(#cqBear)";
            const wick = bull ? "#3ddc97" : "#ff4d6d";
            const bodyTop = y(Math.max(bar.o, bar.c));
            const bodyBot = y(Math.min(bar.o, bar.c));
            const bodyH = Math.max(2, bodyBot - bodyTop);
            return (
              <g key={i}>
                <line
                  x1={x + barW / 2}
                  x2={x + barW / 2}
                  y1={y(bar.h)}
                  y2={y(bar.l)}
                  stroke={wick}
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
                <rect
                  x={x}
                  y={bodyTop}
                  width={barW}
                  height={bodyH}
                  rx="1.5"
                  fill={color}
                />
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
