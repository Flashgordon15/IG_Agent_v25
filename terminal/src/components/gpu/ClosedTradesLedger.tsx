"use client";

import type { ClosedLedgerRow } from "@/hooks/useDeskCapital";

type Props = {
  rows: ClosedLedgerRow[];
  pending: boolean;
  error: string | null;
};

function fmtPx(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v >= 100 ? v.toFixed(1) : v.toFixed(5);
}

function fmtGbp(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
}

function fmtFill(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  // journal stores 0–1 fraction
  const pct = v <= 1 ? v * 100 : v;
  return `${pct.toFixed(1)}%`;
}

function fmtTs(ts: string): string {
  if (!ts) return "—";
  try {
    const d = new Date(ts.includes("T") ? ts : ts.replace(" ", "T") + "Z");
    if (Number.isNaN(d.getTime())) return ts.slice(0, 19);
    return d.toLocaleString("en-GB", {
      hour12: false,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts.slice(0, 19);
  }
}

export function ClosedTradesLedger({ rows, pending, error }: Props) {
  return (
    <section className="gpu-ledger-panel" aria-label="Historical closed ledger">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Settled Session Book</p>
          <h2 className="gpu-panel-title">CLOSED TRADES LEDGER</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip">{rows.length} ROWS</span>
          <span className="gpu-chip">JOURNAL PIPELINE</span>
          {pending ? <span className="gpu-chip">SYNC…</span> : null}
        </div>
      </header>

      {error ? (
        <p className="gpu-ledger-empty" style={{ color: "var(--gpu-amber)" }}>
          {error}
        </p>
      ) : null}

      <div className="gpu-ledger-scroll">
        <table className="gpu-ledger-table">
          <thead>
            <tr>
              <th>TIMESTAMP</th>
              <th>DEAL ID</th>
              <th>DIRECTION</th>
              <th>ENTRY LEVEL</th>
              <th>EXIT LEVEL</th>
              <th>REALIZED RETURN (GBP)</th>
              <th>CLOSING FILL RATE %</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="gpu-ledger-empty">
                  {pending
                    ? "LOADING SETTLED FILLS…"
                    : "NO CLOSED TRADES IN CURRENT SESSION JOURNAL"}
                </td>
              </tr>
            ) : (
              rows.map((r, i) => (
                <tr key={`${r.dealId}-${r.timestamp}-${i}`}>
                  <td className="gpu-ledger-mono">{fmtTs(r.timestamp)}</td>
                  <td className="gpu-ledger-mono">{r.dealId || "—"}</td>
                  <td
                    className={
                      r.direction === "BUY"
                        ? "gpu-metric-val--emerald"
                        : "gpu-tone-amber"
                    }
                  >
                    {r.direction || "—"}
                  </td>
                  <td className="gpu-ledger-mono">{fmtPx(r.entry)}</td>
                  <td className="gpu-ledger-mono">{fmtPx(r.exit)}</td>
                  <td
                    className={
                      r.realizedGbp != null && r.realizedGbp < 0
                        ? "gpu-tone-loss"
                        : "gpu-tone-profit"
                    }
                  >
                    {fmtGbp(r.realizedGbp)}
                  </td>
                  <td className="gpu-ledger-mono">{fmtFill(r.closingFillRate)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
