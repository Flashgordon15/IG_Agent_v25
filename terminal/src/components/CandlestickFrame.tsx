"use client";

import { candleBarsFromHistory } from "@/lib/fulfillment-parse";
import { EPIC_LABELS } from "@/lib/constants";

type Props = {
  epic: string;
  history: number[];
};

export function CandlestickFrame({ epic, history }: Props) {
  const bars = candleBarsFromHistory(history);
  const w = 640;
  const h = 280;
  const pad = 12;
  const prices = bars.flatMap((b) => [b.h, b.l]);
  const min = Math.min(...prices, 0);
  const max = Math.max(...prices, 1);
  const range = max - min || 1;
  const barW = Math.max(4, (w - pad * 2) / Math.max(bars.length, 1) - 2);

  const y = (p: number) => pad + (h - pad * 2) * (1 - (p - min) / range);

  return (
    <div className="cq-panel relative flex h-full min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-[#1f1f24] px-2 py-1">
        <span className="cq-label">Candlestick — {EPIC_LABELS[epic] ?? epic}</span>
        <span className="cq-mono text-[10px] text-[#71717a]">
          {bars.length} bars · live-derived
        </span>
      </div>
      <div className="relative flex-1 overflow-hidden p-2">
        <svg
          viewBox={`0 0 ${w} ${h}`}
          className="h-full w-full"
          preserveAspectRatio="none"
        >
          {bars.map((bar, i) => {
            const x = pad + i * (barW + 2);
            const bull = bar.c >= bar.o;
            const color = bull ? "#00f5d4" : "#ff0055";
            const bodyTop = y(Math.max(bar.o, bar.c));
            const bodyBot = y(Math.min(bar.o, bar.c));
            const bodyH = Math.max(1, bodyBot - bodyTop);
            return (
              <g key={i}>
                <line
                  x1={x + barW / 2}
                  x2={x + barW / 2}
                  y1={y(bar.h)}
                  y2={y(bar.l)}
                  stroke={color}
                  strokeWidth="1"
                />
                <rect
                  x={x}
                  y={bodyTop}
                  width={barW}
                  height={bodyH}
                  fill={color}
                  opacity={0.85}
                />
              </g>
            );
          })}
        </svg>
        <div className="pointer-events-none absolute inset-0 border border-[#1f1f24]/40" />
      </div>
    </div>
  );
}
