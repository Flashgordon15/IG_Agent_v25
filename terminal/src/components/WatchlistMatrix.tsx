"use client";

import { useEffect, useRef, useState } from "react";
import { SysStreamPending } from "@/components/SysStreamPending";
import type { WatchlistRow } from "@/lib/fulfillment-parse";

type Props = {
  rows: WatchlistRow[];
  selectedEpic: string;
  onSelect: (epic: string) => void;
  fault: boolean;
};

const KINETIC_DELTA_PCT = 0.05;
const KINETIC_WINDOW_MS = 20;
const KINETIC_GLOW_MS = 120;
const KINETIC_GLOW_CLASS = "shadow-[0_0_12px_#00f5d4]";

function useKineticGlow(rows: WatchlistRow[]): Set<string> {
  const trailRef = useRef<Map<string, { price: number; ts: number }>>(
    new Map(),
  );
  const [glowEpics, setGlowEpics] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const now = Date.now();
    const next = new Set<string>();
    for (const row of rows) {
      const prev = trailRef.current.get(row.epic);
      if (prev && now - prev.ts <= KINETIC_WINDOW_MS && prev.price > 0) {
        const deltaPct = Math.abs((row.price - prev.price) / prev.price) * 100;
        if (deltaPct >= KINETIC_DELTA_PCT) {
          next.add(row.epic);
        }
      }
      trailRef.current.set(row.epic, { price: row.price, ts: now });
    }
    if (next.size === 0) {
      return;
    }
    setGlowEpics(next);
    const timer = window.setTimeout(
      () => setGlowEpics(new Set()),
      KINETIC_GLOW_MS,
    );
    return () => window.clearTimeout(timer);
  }, [rows]);

  return glowEpics;
}

function Sparkline({
  values,
  up,
  kinetic,
}: {
  values: number[];
  up: boolean;
  kinetic: boolean;
}) {
  const w = 72;
  const h = 22;
  if (values.length < 2) {
    return <svg width={w} height={h} />;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = h - ((v - min) / range) * (h - 4) - 2;
      return `${x},${y}`;
    })
    .join(" ");
  const glow = kinetic
    ? "drop-shadow(0 0 12px rgba(0,245,212,0.95))"
    : up
      ? "drop-shadow(0 0 8px rgba(0,245,212,0.3))"
      : "drop-shadow(0 0 8px rgba(255,0,85,0.3))";
  const stroke = kinetic ? "#00f5d4" : up ? "#00f5d4" : "#ff0055";
  return (
    <svg width={w} height={h} className="shrink-0" style={{ filter: glow }}>
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth={kinetic ? "2" : "1.5"}
        points={pts}
      />
    </svg>
  );
}

function fmtPrice(epic: string, price: number): string {
  if (epic.includes("EURUSD")) return price.toFixed(5);
  if (price > 1000) return price.toFixed(2);
  return price.toFixed(3);
}

export function WatchlistMatrix({
  rows,
  selectedEpic,
  onSelect,
  fault,
}: Props) {
  const kineticEpics = useKineticGlow(rows);

  return (
    <div className="relative cq-panel flex h-full flex-col overflow-hidden">
      <SysStreamPending active={fault} />
      <div className="border-b border-[#1f1f24] px-2 py-1.5">
        <span className="cq-label">Watchlist Matrix</span>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-left">
          <thead className="sticky top-0 bg-[#0f0f12]">
            <tr className="cq-label border-b border-[#1f1f24]">
              <th className="px-2 py-1 font-medium">Ticker</th>
              <th className="px-1 py-1 font-medium">Price</th>
              <th className="px-1 py-1 font-medium">24h</th>
              <th className="px-1 py-1 font-medium" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const up = row.direction === "UP";
              const down = row.direction === "DOWN";
              const selected = row.epic === selectedEpic;
              const kinetic = kineticEpics.has(row.epic);
              return (
                <tr
                  key={row.epic}
                  onClick={() => onSelect(row.epic)}
                  className={`cursor-pointer border-b border-[#1f1f24]/60 hover:bg-[#141418] ${
                    selected ? "bg-[#141418]" : ""
                  } ${kinetic ? KINETIC_GLOW_CLASS : ""}`}
                >
                  <td className="cq-mono px-2 py-1.5 text-[11px] font-semibold text-[#e8ecf4]">
                    {row.ticker}
                  </td>
                  <td
                    className={`cq-mono px-1 py-1.5 text-[11px] transition-shadow duration-75 ${
                      kinetic
                        ? `${KINETIC_GLOW_CLASS} text-[#00f5d4]`
                        : ""
                    }`}
                  >
                    {fmtPrice(row.epic, row.price)}
                  </td>
                  <td
                    className={`cq-mono px-1 py-1.5 text-[10px] font-semibold ${
                      up
                        ? "text-[#00f5d4]"
                        : down
                          ? "text-[#ff0055]"
                          : "text-[#71717a]"
                    }`}
                  >
                    {row.deltaPct >= 0 ? "+" : ""}
                    {row.deltaPct.toFixed(2)}%
                  </td>
                  <td className="px-1 py-1">
                    <Sparkline
                      values={row.history}
                      up={up || !down}
                      kinetic={kinetic}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

