"use client";

import type { PositionAuthorityRow } from "@/lib/quantum-node-types";

type Props = {
  rows: PositionAuthorityRow[];
  totalUplGbp: number | null;
};

function fmtGbp(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "−";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
}

export function ActivePositionAuthorities({ rows, totalUplGbp }: Props) {
  return (
    <section
      className="gpu-ledger-panel"
      aria-label="Active position authorities"
    >
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Hardware-Style Position Ledger</p>
          <h2 className="gpu-panel-title">ACTIVE POSITION AUTHORITIES</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip">{rows.length} OPEN</span>
          <span
            className={`gpu-chip ${
              totalUplGbp != null && totalUplGbp <= -100
                ? "gpu-chip--bad"
                : "gpu-chip--live"
            }`}
          >
            Σ UPL {fmtGbp(totalUplGbp)}
          </span>
          <span className="gpu-chip">B1 FLOOR −£100</span>
        </div>
      </header>

      <div className="gpu-ledger-scroll">
        <table className="gpu-ledger-table">
          <thead>
            <tr>
              <th>DEAL ID</th>
              <th>ASSET</th>
              <th>BIAS</th>
              <th>ENTRY</th>
              <th>3.5× ATR TP</th>
              <th>UPL GBP</th>
              <th>B1 FLOOR</th>
              <th>LAYERS</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="gpu-ledger-empty">
                  NO LIVE AUTHORITIES — FLAT BOOK
                </td>
              </tr>
            ) : (
              rows.map((r) => (
                <tr key={r.dealId}>
                  <td className="gpu-ledger-mono">{r.dealId}</td>
                  <td>
                    <span className="gpu-ledger-asset">{r.label}</span>
                    <span className="gpu-ledger-epic">{r.epic}</span>
                  </td>
                  <td className="gpu-metric-val--emerald">{r.bias}</td>
                  <td className="gpu-ledger-mono">
                    {r.entry > 0 ? r.entry.toFixed(1) : "—"}
                  </td>
                  <td className="gpu-ledger-mono">
                    {r.takeProfit != null ? (
                      <>
                        {r.takeProfit.toFixed(1)}
                        <span className="gpu-ledger-sub">
                          {" "}
                          ({r.atrMultiple.toFixed(1)}× {r.atr.toFixed(2)})
                        </span>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td
                    className={
                      r.pnlGbp != null && r.pnlGbp < 0
                        ? "gpu-tone-amber"
                        : "gpu-metric-val--emerald"
                    }
                  >
                    {fmtGbp(r.pnlGbp)}
                  </td>
                  <td>
                    <span
                      className={`gpu-b1-pill gpu-b1-pill--${r.b1Status.toLowerCase()}`}
                    >
                      {r.b1Status} · {fmtGbp(r.b1FloorGbp)}
                    </span>
                  </td>
                  <td className="gpu-ledger-mono">{r.layers}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
