"use client";

import { useState } from "react";
import { EPIC_LABELS, LEVERAGE_TILES } from "@/lib/constants";

type Props = {
  defaultEpic: string;
};

export function OrderTicket({ defaultEpic }: Props) {
  const [epic, setEpic] = useState(defaultEpic);
  const [size, setSize] = useState("1.0");
  const [direction, setDirection] = useState<"BUY" | "SELL">("BUY");
  const [leverage, setLeverage] = useState<number>(5);
  const [stopPts, setStopPts] = useState("10");

  const epics = Object.keys(EPIC_LABELS);

  return (
    <div className="cq-panel flex flex-1 flex-col overflow-hidden">
      <div className="border-b border-[#1f1f24] px-2 py-1">
        <span className="cq-label">Order Ticket</span>
      </div>
      <div className="flex flex-1 flex-col gap-2 p-2">
        <label className="flex flex-col gap-1">
          <span className="cq-label">Instrument</span>
          <select
            value={epic}
            onChange={(e) => setEpic(e.target.value)}
            className="cq-mono rounded-none border border-[#1f1f24] bg-[#050505] px-2 py-1.5 text-[11px] text-[#e8ecf4] outline-none focus:border-[#00f5d4]"
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
            className={`cq-mono rounded-none border px-2 py-2 text-[11px] font-bold transition-colors ${
              direction === "BUY"
                ? "border-[#00f5d4] bg-[#00f5d4]/15 text-[#00f5d4]"
                : "border-[#1f1f24] text-[#71717a] hover:bg-[#141418]"
            }`}
          >
            BUY
          </button>
          <button
            type="button"
            onClick={() => setDirection("SELL")}
            className={`cq-mono rounded-none border px-2 py-2 text-[11px] font-bold transition-colors ${
              direction === "SELL"
                ? "border-[#ff0055] bg-[#ff0055]/15 text-[#ff0055]"
                : "border-[#1f1f24] text-[#71717a] hover:bg-[#141418]"
            }`}
          >
            SELL
          </button>
        </div>

        <label className="flex flex-col gap-1">
          <span className="cq-label">Size (lots)</span>
          <input
            type="text"
            value={size}
            onChange={(e) => setSize(e.target.value)}
            className="cq-mono rounded-none border border-[#1f1f24] bg-[#050505] px-2 py-1.5 text-[11px] outline-none focus:border-[#00f5d4]"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="cq-label">Stop (pts)</span>
          <input
            type="text"
            value={stopPts}
            onChange={(e) => setStopPts(e.target.value)}
            className="cq-mono rounded-none border border-[#1f1f24] bg-[#050505] px-2 py-1.5 text-[11px] outline-none focus:border-[#00f5d4]"
          />
        </label>

        <div>
          <span className="cq-label mb-1 block">Leverage</span>
          <div className="grid grid-cols-4 gap-1">
            {LEVERAGE_TILES.map((x) => (
              <button
                key={x}
                type="button"
                onClick={() => setLeverage(x)}
                className={`cq-mono rounded-none border py-1.5 text-[10px] font-semibold transition-colors ${
                  leverage === x
                    ? "border-[#00f5d4] bg-[#00f5d4]/20 text-[#00f5d4]"
                    : "border-[#1f1f24] text-[#71717a] hover:bg-[#00f5d4]/10 hover:text-[#00f5d4]"
                }`}
              >
                [{x}x]
              </button>
            ))}
          </div>
        </div>

        <button
          type="button"
          className={`cq-mono mt-auto rounded-none border py-2 text-[11px] font-bold transition-colors ${
            direction === "BUY"
              ? "border-[#00f5d4] bg-[#00f5d4]/10 text-[#00f5d4] hover:bg-[#00f5d4]/20"
              : "border-[#ff0055] bg-[#ff0055]/10 text-[#ff0055] hover:bg-[#ff0055]/20"
          }`}
        >
          ARM {direction} · {EPIC_LABELS[epic] ?? epic} · {leverage}x
        </button>
      </div>
    </div>
  );
}
