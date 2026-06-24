"use client";

import { SysStreamPending } from "@/components/SysStreamPending";

type Props = {
  labels: string[];
  matrix: number[][];
  fault: boolean;
};

function cellColor(v: number): string {
  if (v >= 0.65) return "text-[#00f5d4] bg-[#00f5d4]/10";
  if (v <= -0.35) return "text-[#ff0055] bg-[#ff0055]/10";
  return "text-[#e8ecf4] bg-[#141418]";
}

export function CorrelationMatrix({ labels, matrix, fault }: Props) {
  return (
    <div className="relative cq-panel overflow-hidden">
      <SysStreamPending active={fault} />
      <div className="border-b border-[#1f1f24] px-2 py-1">
        <span className="cq-label">Asset Correlation</span>
      </div>
      <div className="overflow-auto p-1">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="cq-label px-1 py-1" />
              {labels.map((l) => (
                <th key={l} className="cq-label px-1 py-1 font-medium">
                  {l}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {labels.map((rowLabel, i) => (
              <tr key={rowLabel}>
                <td className="cq-label px-1 py-1 font-medium">{rowLabel}</td>
                {matrix[i]?.map((v, j) => (
                  <td key={`${i}-${j}`} className="p-0.5">
                    <div
                      className={`cq-mono flex h-8 items-center justify-center text-[10px] font-semibold ${cellColor(v)}`}
                    >
                      {v.toFixed(2)}
                    </div>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
