"use client";

import { SysStreamPending } from "@/components/SysStreamPending";
import type { LedgerRow } from "@/lib/fulfillment-parse";

type Props = {
  rows: LedgerRow[];
  fault: boolean;
};

export function TransactionLedger({ rows, fault }: Props) {
  return (
    <div className="relative cq-panel flex h-full min-h-0 flex-col overflow-hidden">
      <SysStreamPending active={fault} />
      <div className="border-b border-[#1f1f24] px-2 py-1">
        <span className="cq-label">Transaction Ledger</span>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-left">
          <thead className="sticky top-0 bg-[#0f0f12]">
            <tr className="cq-label border-b border-[#1f1f24]">
              <th className="px-2 py-1 font-medium">Timestamp</th>
              <th className="px-2 py-1 font-medium">Side</th>
              <th className="px-2 py-1 font-medium">Size</th>
              <th className="px-2 py-1 font-medium">Venue</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={4} className="cq-mono px-2 py-3 text-[10px] text-[#71717a]">
                  No fills in rolling window
                </td>
              </tr>
            )}
            {rows.map((row, i) => (
              <tr
                key={`${row.timestamp}-${row.epic}-${i}`}
                className="border-b border-[#1f1f24]/50 hover:bg-[#141418]"
              >
                <td className="cq-mono px-2 py-1 text-[10px] text-[#e8ecf4]">
                  {row.timestamp}
                </td>
                <td className="px-2 py-1">
                  <span
                    className={`cq-mono rounded-none px-1.5 py-0.5 text-[10px] font-bold ${
                      row.side === "BUY"
                        ? "bg-[#00f5d4]/15 text-[#00f5d4]"
                        : "bg-[#ff0055]/15 text-[#ff0055]"
                    }`}
                  >
                    {row.side}
                  </span>
                </td>
                <td className="cq-mono px-2 py-1 text-[10px]">
                  {row.size.toFixed(2)}
                </td>
                <td className="cq-mono px-2 py-1 text-[10px] text-[#71717a]">
                  [{row.venue}]
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
