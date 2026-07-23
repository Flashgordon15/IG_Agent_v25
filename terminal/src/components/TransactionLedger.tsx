"use client";

import { SysStreamPending } from "@/components/SysStreamPending";
import type { LedgerRow } from "@/lib/fulfillment-parse";

type Props = {
  rows: LedgerRow[];
  fault: boolean;
};

export function TransactionLedger({ rows, fault }: Props) {
  return (
    <div className="cq-panel relative flex h-full min-h-0 flex-col overflow-hidden">
      <SysStreamPending active={fault} />
      <div
        className="border-b border-[var(--color-desk-line)]"
        style={{ padding: "0.7rem 1rem" }}
      >
        <div className="cq-label">Ledger</div>
        <div className="mt-1 text-[length:var(--desk-type-sm)] font-semibold">
          Recent fills
        </div>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-left">
          <thead className="sticky top-0 bg-[var(--color-desk-panel)]">
            <tr className="cq-label border-b border-[var(--color-desk-line)]">
              <th className="px-3 py-2 font-medium">Time</th>
              <th className="px-2 py-2 font-medium">Side</th>
              <th className="px-2 py-2 font-medium">Size</th>
              <th className="px-2 py-2 font-medium">Venue</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={4}
                  className="cq-mono px-3 py-4 text-[length:var(--desk-type-sm)] text-[var(--color-desk-mute)]"
                >
                  No fills in rolling window
                </td>
              </tr>
            )}
            {rows.map((row, i) => (
              <tr
                key={`${row.timestamp}-${row.epic}-${i}`}
                className="cq-row border-b border-[var(--color-desk-line)]"
              >
                <td className="cq-mono px-3 py-2 text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
                  {row.timestamp}
                </td>
                <td className="px-2 py-2">
                  <span
                    className={`cq-mono rounded-md px-2 py-0.5 text-[length:var(--desk-type-xs)] font-semibold ${
                      row.side === "BUY"
                        ? "bg-[rgba(61,220,151,0.12)] text-[var(--color-desk-good)]"
                        : "bg-[rgba(255,77,109,0.12)] text-[var(--color-desk-bad)]"
                    }`}
                  >
                    {row.side}
                  </span>
                </td>
                <td className="cq-mono px-2 py-2 text-[length:var(--desk-type-sm)]">
                  {row.size}
                </td>
                <td className="cq-mono px-2 py-2 text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
                  {row.venue}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
