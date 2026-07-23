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
    <div className="flex flex-1 flex-col justify-center gap-1 overflow-auto px-2 py-2">
      {levels.map((lvl) => (
        <div
          key={`${side}-${lvl.label}`}
          className="relative overflow-hidden rounded-md px-2 py-1.5"
        >
          <div
            className={`absolute inset-y-0 ${isAsk ? "right-0" : "left-0"} ${
              isAsk
                ? "bg-[rgba(255,77,109,0.14)]"
                : "bg-[rgba(61,220,151,0.14)]"
            }`}
            style={{
              width: `${Math.max(8, lvl.pct)}%`,
              transition: "width 160ms ease",
            }}
          />
          <div className="relative flex items-center justify-between gap-2">
            <span className="cq-mono truncate text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
              {lvl.label}
            </span>
            <span
              className={`cq-mono text-[length:var(--desk-type-sm)] font-semibold ${
                isAsk
                  ? "text-[var(--color-desk-bad)]"
                  : "text-[var(--color-desk-good)]"
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
    <div className="cq-panel flex h-full w-[clamp(10.5rem,12vw,13.5rem)] shrink-0 flex-col overflow-hidden">
      <div
        className="border-b border-[var(--color-desk-line)]"
        style={{ padding: "0.7rem 0.85rem" }}
      >
        <div className="cq-label">Depth</div>
        <div className="mt-1 text-[length:var(--desk-type-sm)] font-semibold">
          {EPIC_LABELS[epic] ?? "—"}
        </div>
      </div>
      <div className="cq-label px-3 py-1 text-center text-[var(--color-desk-bad)]">
        ASK
      </div>
      <LadderSide levels={asks} side="ask" />
      <div className="border-y border-[var(--color-desk-line)] bg-[var(--color-desk-elevated)] px-3 py-2 text-center">
        <span className="cq-mono text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
          MID
        </span>
      </div>
      <div className="cq-label px-3 py-1 text-center text-[var(--color-desk-good)]">
        BID
      </div>
      <LadderSide levels={bids} side="bid" />
    </div>
  );
}
