"use client";

import { useState } from "react";
import { EPIC_LABELS, LEVERAGE_TILES } from "@/lib/constants";

type Props = {
  defaultEpic: string;
};

const fieldClass =
  "cq-mono w-full rounded-[calc(var(--desk-radius)-4px)] border border-[var(--color-desk-line)] bg-[var(--color-desk-void)] px-3 py-2 text-[length:var(--desk-type-sm)] text-[var(--color-desk-text)] outline-none transition focus:border-[rgba(61,220,151,0.45)]";

export function OrderTicket({ defaultEpic }: Props) {
  const [epic, setEpic] = useState(defaultEpic);
  const [size, setSize] = useState("1.0");
  const [direction, setDirection] = useState<"BUY" | "SELL">("BUY");
  const [leverage, setLeverage] = useState<number>(5);
  const [stopPts, setStopPts] = useState("10");

  const epics = Object.keys(EPIC_LABELS);

  return (
    <div className="cq-panel flex h-full flex-1 flex-col overflow-hidden">
      <div
        className="border-b border-[var(--color-desk-line)]"
        style={{ padding: "0.65rem 0.9rem" }}
      >
        <div className="cq-label">Order ticket</div>
      </div>
      <div className="flex flex-1 flex-col gap-3 overflow-auto p-3">
        <label className="flex flex-col gap-1.5">
          <span className="cq-label">Instrument</span>
          <select
            value={epic}
            onChange={(e) => setEpic(e.target.value)}
            className={fieldClass}
          >
            {epics.map((e) => (
              <option key={e} value={e}>
                {EPIC_LABELS[e]}
              </option>
            ))}
          </select>
        </label>

        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setDirection("BUY")}
            className={`cq-mono rounded-[calc(var(--desk-radius)-4px)] border px-2 py-2.5 text-[length:var(--desk-type-sm)] font-bold transition ${
              direction === "BUY"
                ? "border-[rgba(61,220,151,0.55)] bg-[rgba(61,220,151,0.12)] text-[var(--color-desk-good)]"
                : "border-[var(--color-desk-line)] text-[var(--color-desk-mute)] hover:bg-white/3"
            }`}
          >
            BUY
          </button>
          <button
            type="button"
            onClick={() => setDirection("SELL")}
            className={`cq-mono rounded-[calc(var(--desk-radius)-4px)] border px-2 py-2.5 text-[length:var(--desk-type-sm)] font-bold transition ${
              direction === "SELL"
                ? "border-[rgba(255,77,109,0.55)] bg-[rgba(255,77,109,0.12)] text-[var(--color-desk-bad)]"
                : "border-[var(--color-desk-line)] text-[var(--color-desk-mute)] hover:bg-white/3"
            }`}
          >
            SELL
          </button>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <label className="flex flex-col gap-1.5">
            <span className="cq-label">Size</span>
            <input
              type="text"
              value={size}
              onChange={(e) => setSize(e.target.value)}
              className={fieldClass}
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="cq-label">Stop (pts)</span>
            <input
              type="text"
              value={stopPts}
              onChange={(e) => setStopPts(e.target.value)}
              className={fieldClass}
            />
          </label>
        </div>

        <div>
          <span className="cq-label mb-1.5 block">Leverage</span>
          <div className="grid grid-cols-4 gap-1.5">
            {LEVERAGE_TILES.map((x) => (
              <button
                key={x}
                type="button"
                onClick={() => setLeverage(x)}
                className={`cq-mono rounded-[calc(var(--desk-radius)-4px)] border py-2 text-[length:var(--desk-type-xs)] font-semibold transition ${
                  leverage === x
                    ? "border-[rgba(91,141,239,0.55)] bg-[rgba(91,141,239,0.14)] text-[var(--color-desk-info)]"
                    : "border-[var(--color-desk-line)] text-[var(--color-desk-mute)] hover:bg-white/3"
                }`}
              >
                {x}×
              </button>
            ))}
          </div>
        </div>

        <button
          type="button"
          className={`cq-mono mt-auto rounded-[calc(var(--desk-radius)-2px)] border py-2.5 text-[length:var(--desk-type-sm)] font-bold tracking-wide transition ${
            direction === "BUY"
              ? "border-[rgba(61,220,151,0.5)] bg-[rgba(61,220,151,0.1)] text-[var(--color-desk-good)] hover:bg-[rgba(61,220,151,0.16)]"
              : "border-[rgba(255,77,109,0.5)] bg-[rgba(255,77,109,0.1)] text-[var(--color-desk-bad)] hover:bg-[rgba(255,77,109,0.16)]"
          }`}
        >
          ARM {direction} · {EPIC_LABELS[epic] ?? epic} · {leverage}×
        </button>
      </div>
    </div>
  );
}
