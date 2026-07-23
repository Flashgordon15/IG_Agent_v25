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
const KINETIC_GLOW_MS = 140;

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
        if (deltaPct >= KINETIC_DELTA_PCT) next.add(row.epic);
      }
      trailRef.current.set(row.epic, { price: row.price, ts: now });
    }
    if (next.size === 0) return;
    setGlowEpics(next);
    const timer = window.setTimeout(() => setGlowEpics(new Set()), KINETIC_GLOW_MS);
    return () => window.clearTimeout(timer);
  }, [rows]);

  return glowEpics;
}

function Sparkline({ values, up }: { values: number[]; up: boolean }) {
  const w = 88;
  const h = 28;
  if (values.length < 2) return <svg width={w} height={h} />;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 6) - 3;
    return `${x},${y}`;
  });
  const line = pts.join(" ");
  const area = `${line} L ${w},${h} L 0,${h} Z`;
  const stroke = up ? "#3ddc97" : "#ff4d6d";
  const fill = up ? "rgba(61,220,151,0.16)" : "rgba(255,77,109,0.14)";
  return (
    <svg width={w} height={h} className="shrink-0" aria-hidden>
      <path d={area} fill={fill} />
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth="1.75"
        strokeLinejoin="round"
        strokeLinecap="round"
        points={line}
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
    <div className="cq-panel relative flex h-full flex-col overflow-hidden">
      <SysStreamPending active={fault} />
      <div
        className="border-b border-[var(--color-desk-line)]"
        style={{ padding: "0.75rem 1rem" }}
      >
        <div className="cq-label">Watchlist</div>
        <div className="mt-1 text-[length:clamp(0.95rem,0.85rem+0.2vw,1.1rem)] font-semibold">
          Markets
        </div>
      </div>
      <div className="flex-1 overflow-auto">
        <div className="flex flex-col p-1.5">
          {rows.map((row) => {
            const up = row.direction === "UP";
            const down = row.direction === "DOWN";
            const selected = row.epic === selectedEpic;
            const kinetic = kineticEpics.has(row.epic);
            return (
              <button
                key={row.epic}
                type="button"
                onClick={() => onSelect(row.epic)}
                className={`cq-row flex w-full items-center gap-2 rounded-[calc(var(--desk-radius)-4px)] px-2.5 py-2.5 text-left ${
                  selected ? "cq-row-selected" : ""
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="cq-mono text-[length:var(--desk-type-sm)] font-semibold">
                    {row.ticker}
                  </div>
                  <div
                    className={`cq-mono text-[length:clamp(0.85rem,0.75rem+0.2vw,1.05rem)] font-semibold ${
                      kinetic
                        ? "text-[var(--color-desk-good)]"
                        : "text-[var(--color-desk-text)]"
                    }`}
                  >
                    {fmtPrice(row.epic, row.price)}
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1">
                  <span
                    className={`cq-mono text-[length:var(--desk-type-xs)] font-semibold ${
                      up
                        ? "text-[var(--color-desk-good)]"
                        : down
                          ? "text-[var(--color-desk-bad)]"
                          : "text-[var(--color-desk-mute)]"
                    }`}
                  >
                    {row.deltaPct >= 0 ? "+" : ""}
                    {row.deltaPct.toFixed(2)}%
                  </span>
                  <Sparkline values={row.history} up={up || !down} />
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
