"use client";

import { SysStreamPending } from "@/components/SysStreamPending";

type Props = {
  labels: string[];
  matrix: number[][];
  fault: boolean;
};

function cellStyle(v: number): { bg: string; fg: string } {
  if (v >= 0.65) {
    return { bg: "rgba(61,220,151,0.16)", fg: "#3ddc97" };
  }
  if (v <= -0.35) {
    return { bg: "rgba(255,77,109,0.14)", fg: "#ff4d6d" };
  }
  const a = Math.min(0.12, Math.abs(v) * 0.12);
  return { bg: `rgba(148,163,184,${a})`, fg: "#e8eef7" };
}

export function CorrelationMatrix({ labels, matrix, fault }: Props) {
  return (
    <div className="cq-panel relative h-full overflow-hidden">
      <SysStreamPending active={fault} />
      <div
        className="border-b border-[var(--color-desk-line)]"
        style={{ padding: "0.65rem 0.9rem" }}
      >
        <div className="cq-label">Correlation</div>
      </div>
      <div className="overflow-auto p-2">
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
                {matrix[i]?.map((v, j) => {
                  const s = cellStyle(v);
                  return (
                    <td key={`${i}-${j}`} className="p-1">
                      <div
                        className="cq-mono flex h-9 items-center justify-center rounded-md text-[length:var(--desk-type-xs)] font-semibold"
                        style={{ background: s.bg, color: s.fg }}
                      >
                        {v.toFixed(2)}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
