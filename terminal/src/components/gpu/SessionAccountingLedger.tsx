"use client";

import { useMemo, useState } from "react";
import type {
  ClosedLedgerRow,
  DeskCapitalView,
  LedgerQuality,
} from "@/hooks/useDeskCapital";

type Props = {
  capital: DeskCapitalView;
};

type Filter = "all" | "settled" | "bridged" | "cancelled";

function fmtPx(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v >= 100 ? v.toFixed(1) : v.toFixed(5);
}

function fmtGbp(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
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

function qualityBadge(q: LedgerQuality | undefined, result?: string): string {
  if (q === "bridged") return "BRIDGED";
  if (result) return result.toUpperCase();
  if (q === "cancelled") return "CANCELLED";
  if (q === "flat") return "FLAT";
  if (q === "settled") return "SETTLED";
  return "UNKNOWN";
}

function seriesPath(
  pts: number[],
  w: number,
  h: number,
  min: number,
  max: number,
  flat: boolean,
): string {
  const span = Math.max(1e-6, max - min);
  return pts
    .map((v, i) => {
      const x = (i / Math.max(1, pts.length - 1)) * (w - 4) + 2;
      const y = flat
        ? h / 2
        : 2 + (1 - (v - min) / span) * (h - 4);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function EquitySpark({
  series,
  provisionalSeries,
  realizedGbp,
}: {
  series: number[];
  provisionalSeries: number[];
  realizedGbp: number;
}) {
  const w = 220;
  const h = 48;
  // Force true £0.00 baseline when session cash is flat (no stale −£132 curves)
  const forceFlat = !Number.isFinite(realizedGbp) || Math.abs(realizedGbp) < 1e-9;
  const settled = forceFlat ? [0, 0] : series.length ? series : [0, 0];
  const provisional = provisionalSeries.length ? provisionalSeries : [0, 0];
  const provActive = provisional.some((v) => Math.abs(v) > 1e-9);
  const all = provActive ? [...settled, ...provisional] : settled;
  const min = Math.min(...all);
  const max = Math.max(...all);
  const flat =
    forceFlat || settled.every((v) => Math.abs(v - settled[0]) < 1e-9);
  const settledPath = seriesPath(settled, w, h, min, max, flat && !provActive);
  const provisionalPath = seriesPath(
    provisional,
    w,
    h,
    min,
    max,
    !provActive,
  );
  const last = settled[settled.length - 1] ?? 0;
  const stroke =
    last > 0 ? "var(--gpu-emerald)" : last < 0 ? "var(--gpu-danger)" : "var(--gpu-mute)";

  return (
    <div className="equity-spark-wrap" aria-label="Session equity path">
      <span className="gpu-metric-key">SETTLED CASH · PROVISIONAL LANE</span>
      <svg viewBox={`0 0 ${w} ${h}`} className="equity-spark" role="img">
        <line
          x1={2}
          x2={w - 2}
          y1={h / 2}
          y2={h / 2}
          stroke="rgba(148,163,184,0.15)"
        />
        {provActive ? (
          <path
            d={provisionalPath}
            fill="none"
            stroke="rgba(251, 191, 36, 0.85)"
            strokeWidth="1.5"
            strokeDasharray="3 3"
            className="equity-spark-provisional"
          />
        ) : null}
        <path d={settledPath} fill="none" stroke={stroke} strokeWidth="1.8" />
      </svg>
      <span className="equity-spark-label">
        {flat
          ? "£0.00 SETTLED BASELINE"
          : `SETTLED ${fmtGbp(last)}`}
        {provActive
          ? ` · PROV ${fmtGbp(provisional[provisional.length - 1] ?? 0)}`
          : ""}
      </span>
    </div>
  );
}

function AssetStack({ capital }: { capital: DeskCapitalView }) {
  if (!capital.assetStack.length) {
    return (
      <div className="asset-stack asset-stack--empty">
        <span className="gpu-metric-key">PER-ASSET STACK</span>
        <span className="gpu-capital-sub">No asset contribution yet</span>
      </div>
    );
  }
  return (
    <div className="asset-stack" aria-label="Per-asset P&L stack">
      <span className="gpu-metric-key">PER-ASSET STACK</span>
      {capital.assetStack.slice(0, 6).map((a) => (
        <div key={a.label} className="asset-stack-row">
          <div className="asset-stack-meta">
            <strong>{a.label}</strong>
            <span>
              cash {fmtGbp(a.settledGbp)} · open {fmtGbp(a.openUplGbp)}
            </span>
          </div>
          <div className="asset-stack-track">
            <div
              className="asset-stack-fill asset-stack-fill--cash"
              style={{
                width: `${Math.min(
                  100,
                  Math.max(
                    0,
                    (Math.abs(a.settledGbp) /
                      Math.max(
                        1e-6,
                        Math.abs(a.settledGbp) + Math.abs(a.openUplGbp),
                      )) *
                      a.pct,
                  ),
                )}%`,
              }}
            />
            <div
              className="asset-stack-fill asset-stack-fill--open"
              style={{
                width: `${Math.min(100, Math.max(0, a.pct))}%`,
              }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

function slipTone(deltaPts: number | null): string {
  if (deltaPts == null) return "";
  const abs = Math.abs(deltaPts);
  if (abs <= 2) return "gpu-metric-val--emerald";
  if (abs <= 8) return "gpu-tone-amber";
  return "gpu-tone-loss";
}

function PnlCell({ row }: { row: ClosedLedgerRow }) {
  const q = row.quality || "unknown";
  if (q === "cancelled" || q === "flat") {
    return (
      <td className="ledger-pnl ledger-pnl--muted">
        <span className="ledger-pnl-main">—</span>
        <span className="ledger-pnl-sub">no cash settle</span>
      </td>
    );
  }
  if (row.realizedGbp == null) {
    return (
      <td className="ledger-pnl ledger-pnl--muted">
        <span className="ledger-pnl-main">—</span>
        <span className="ledger-pnl-sub">pending</span>
      </td>
    );
  }
  const tone =
    row.realizedGbp > 0
      ? "gpu-tone-profit"
      : row.realizedGbp < 0
        ? "gpu-tone-loss"
        : "ledger-pnl--muted";
  return (
    <td className={`ledger-pnl ${tone}`}>
      <span className="ledger-pnl-main">{fmtGbp(row.realizedGbp)}</span>
      <span className="ledger-pnl-sub">
        {q === "bridged"
          ? `last live UPL${row.lastLiveUpl != null ? ` ${fmtGbp(row.lastLiveUpl)}` : ""}`
          : row.pnlPoints != null
            ? `${row.pnlPoints >= 0 ? "+" : ""}${row.pnlPoints.toFixed(2)} pts`
            : "settled"}
      </span>
    </td>
  );
}

function LedgerTable({
  rows,
  empty,
}: {
  rows: ClosedLedgerRow[];
  empty: string;
}) {
  return (
    <div className="gpu-ledger-scroll">
      <table className="gpu-ledger-table">
        <thead>
          <tr>
            <th>TIMESTAMP</th>
            <th>DEAL ID</th>
            <th>ASSET</th>
            <th>DIR</th>
            <th>RESULT</th>
            <th>ENTRY</th>
            <th>EXIT</th>
            <th>Δ PTS</th>
            <th>SIZE</th>
            <th>FILL %</th>
            <th>NET REALIZED P&amp;L</th>
            <th>CUMUL</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={12} className="gpu-ledger-empty">
                {empty}
              </td>
            </tr>
          ) : (
            rows.map((r, i) => (
              <tr
                key={`${r.dealId}-${r.timestamp}-${i}`}
                className={
                  r.quality === "cancelled" || r.quality === "flat"
                    ? "ledger-row--muted"
                    : r.quality === "bridged"
                      ? "ledger-row--bridged"
                      : undefined
                }
              >
                <td className="gpu-ledger-mono">{fmtTs(r.timestamp)}</td>
                <td className="gpu-ledger-mono">{r.dealId || "—"}</td>
                <td className="gpu-ledger-mono">{r.market || "—"}</td>
                <td
                  className={
                    r.direction === "BUY" || r.direction === "LONG"
                      ? "gpu-metric-val--emerald"
                      : r.direction
                        ? "sniper-flag--short-text"
                        : ""
                  }
                >
                  {r.direction || "—"}
                </td>
                <td>
                  <span
                    className={`ledger-result ledger-result--${r.quality || "unknown"}`}
                  >
                    {qualityBadge(r.quality, r.result)}
                  </span>
                </td>
                <td className="gpu-ledger-mono">{fmtPx(r.entry)}</td>
                <td className="gpu-ledger-mono">{fmtPx(r.exit)}</td>
                <td className={`gpu-ledger-mono ${slipTone(r.deltaPts ?? null)}`}>
                  {r.deltaPts != null
                    ? `${r.deltaPts >= 0 ? "+" : ""}${r.deltaPts.toFixed(1)}`
                    : "—"}
                </td>
                <td className="gpu-ledger-mono">
                  {r.size != null ? r.size.toFixed(2) : "—"}
                </td>
                <td className="gpu-ledger-mono">
                  {r.closingFillRate != null
                    ? `${(r.closingFillRate <= 1 ? r.closingFillRate * 100 : r.closingFillRate).toFixed(0)}%`
                    : "—"}
                </td>
                <PnlCell row={r} />
                <td className="gpu-ledger-mono">
                  {r.cumulativeGbp != null ? fmtGbp(r.cumulativeGbp) : "—"}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

export function SessionAccountingLedger({ capital }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const wr =
    capital.winRate == null ? "—" : `${(capital.winRate * 100).toFixed(1)}%`;
  const tone =
    capital.realizedTodayGbp > 0
      ? "gpu-tone-profit"
      : capital.realizedTodayGbp < 0
        ? "gpu-tone-loss"
        : "";

  const filtered = useMemo(() => {
    if (filter === "settled") {
      return capital.closedRows.filter((r) => r.quality === "settled");
    }
    if (filter === "bridged") {
      return capital.closedRows.filter((r) => r.quality === "bridged");
    }
    if (filter === "cancelled") {
      return capital.closedRows.filter(
        (r) => r.quality === "cancelled" || r.quality === "flat",
      );
    }
    return capital.closedRows;
  }, [capital.closedRows, filter]);

  return (
    <section className="session-ledger" aria-label="Session accounting ledger">
      <header className="session-ledger-ticker">
        <div className="session-ticker-block">
          <span className="gpu-metric-key">TOTAL REALIZED CASH FLOW TODAY</span>
          <strong className={`session-ticker-value ${tone}`}>
            {fmtGbp(capital.realizedTodayGbp)}
          </strong>
          <span className="gpu-capital-sub">
            {capital.settledCount} settled
            {capital.bridgedCount > 0
              ? ` · ${capital.bridgedCount} bridged`
              : ""}
            {" · "}
            {capital.cancelledCount} cancelled/flat
          </span>
        </div>
        <div className="session-ticker-block">
          <span className="gpu-metric-key">CURRENT WIN RATE %</span>
          <strong className="session-ticker-value">
            {wr}
            <span className="session-ticker-sub">
              {" "}
              · {capital.wins}W / {capital.losses}L
            </span>
          </strong>
        </div>
        <div className="session-ticker-block session-ticker-block--bar">
          <span className="gpu-metric-key">£1,000 DAILY MILESTONE</span>
          <strong className="session-ticker-value session-ticker-value--sm">
            £{Math.abs(capital.realizedTodayGbp).toFixed(2)} / £
            {capital.milestoneGbp.toLocaleString("en-GB", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            })}
          </strong>
          <div className="gpu-milestone-track session-milestone" aria-hidden>
            <div
              className="gpu-milestone-fill"
              style={{
                width: `${Math.min(100, Math.max(0, capital.progressPct))}%`,
              }}
            />
          </div>
        </div>
        <EquitySpark
          series={capital.equitySeries}
          provisionalSeries={capital.provisionalEquitySeries}
          realizedGbp={capital.realizedTodayGbp}
        />
      </header>

      <div className="session-ledger-extras">
        <AssetStack capital={capital} />
      </div>

      <div className="session-ledger-body">
        <div className="gpu-panel-head">
          <div>
            <p className="gpu-kicker">Settled Session Book</p>
            <h2 className="gpu-panel-title">CLOSED TRADES &amp; CASH ACCOUNTING</h2>
          </div>
          <div className="gpu-tensor-chips">
            {(
              [
                ["all", `ALL ${capital.closedRows.length}`],
                ["settled", `SETTLED ${capital.settledCount}`],
                ...(capital.bridgedCount > 0
                  ? [["bridged", `BRIDGED ${capital.bridgedCount}`]]
                  : []),
                ["cancelled", `CANCELLED ${capital.cancelledCount}`],
              ] as Array<[Filter, string]>
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={`gpu-chip ${filter === key ? "gpu-chip--live" : ""}`}
                onClick={() => setFilter(key)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {capital.dataNote ? (
          <p className="ledger-data-note">{capital.dataNote}</p>
        ) : null}
        {capital.error ? (
          <p className="gpu-ledger-empty" style={{ color: "var(--gpu-amber)" }}>
            {capital.error}
          </p>
        ) : null}

        <LedgerTable
          rows={filtered}
          empty={
            capital.pending
              ? "LOADING SETTLED CASH FLOWS…"
              : filter === "settled"
                ? "NO SETTLED WIN/LOSS FILLS TODAY"
                : filter === "bridged"
                  ? "NO BRIDGED UPL CLOSES YET"
                  : "NO CLOSED TRADES IN CURRENT SESSION"
          }
        />

        {capital.recentCashRows.length > 0 && capital.settledCount === 0 ? (
          <div className="ledger-history-block">
            <p className="gpu-kicker">Recent Historical Cash (not today)</p>
            <LedgerTable
              rows={capital.recentCashRows}
              empty="NO HISTORICAL CASH ROWS"
            />
          </div>
        ) : null}
      </div>
    </section>
  );
}
