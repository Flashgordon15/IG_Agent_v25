"use client";

import type { DepthLevel } from "@/lib/fulfillment-parse";
import { EPIC_LABELS } from "@/lib/constants";

type Props = {
  epic: string;
  asks: DepthLevel[];
  bids: DepthLevel[];
};

function LadderSide({
  levels,
  side,
}: {
  levels: DepthLevel[];
  side: "ask" | "bid";
}) {
  const isAsk = side === "ask";
  return (
    <div className="flex flex-1 flex-col gap-0.5 overflow-auto py-1">
      {levels.map((lvl) => (
        <div key={`${side}-${lvl.label}`} className="relative px-1">
          <div
            className={`absolute inset-y-0 ${isAsk ? "right-0" : "left-0"} ${
              isAsk ? "bg-[#ff0055]/10" : "bg-[#00f5d4]/10"
            }`}
            style={{ width: `${lvl.pct}%` }}
          />
          <div className="relative flex items-center justify-between gap-1 py-0.5">
            <span className="cq-mono truncate text-[9px] text-[#71717a]">
              {lvl.label}
            </span>
            <span
              className={`cq-mono text-[10px] font-semibold ${
                isAsk ? "text-[#ff0055]" : "text-[#00f5d4]"
              }`}
            >
              {lvl.weight.toFixed(0)}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

export function OrderBookLadder({ epic, asks, bids }: Props) {
  return (
    <div className="cq-panel flex h-full w-44 shrink-0 flex-col overflow-hidden">
      <div className="border-b border-[#1f1f24] px-2 py-1">
        <span className="cq-label">Depth — {EPIC_LABELS[epic] ?? "—"}</span>
      </div>
      <div className="cq-label border-b border-[#1f1f24] px-2 py-0.5 text-center text-[#ff0055]">
        ASK
      </div>
      <LadderSide levels={asks} side="ask" />
      <div className="border-y border-[#1f1f24] bg-[#141418] px-2 py-1 text-center">
        <span className="cq-mono text-[10px] text-[#e8ecf4]">MID</span>
      </div>
      <div className="cq-label border-b border-[#1f1f24] px-2 py-0.5 text-center text-[#00f5d4]">
        BID
      </div>
      <LadderSide levels={bids} side="bid" />
    </div>
  );
}
