"use client";

import type { DeskCapitalView } from "@/hooks/useDeskCapital";

type Props = {
  capital: DeskCapitalView;
};

function fmtGbp(v: number, digits = 2): string {
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(digits)}`;
}

function toneClass(v: number): string {
  if (v > 0) return "gpu-tone-profit";
  if (v < 0) return "gpu-tone-loss";
  return "";
}

export function CapitalAccountingRow({ capital }: Props) {
  const wr =
    capital.winRate == null ? "—" : `${(capital.winRate * 100).toFixed(1)}%`;
  const pf =
    capital.profitFactor == null
      ? "—"
      : capital.profitFactor >= 99
        ? "∞"
        : capital.profitFactor.toFixed(2);

  return (
    <section className="gpu-capital-row" aria-label="Capital accounting">
      <article className="gpu-capital-card">
        <span className="gpu-metric-key">TOTAL REALIZED P&L TODAY</span>
        <strong className={`gpu-capital-value ${toneClass(capital.realizedTodayGbp)}`}>
          {fmtGbp(capital.realizedTodayGbp)}
        </strong>
        <span className="gpu-capital-sub">
          session settled · bias {capital.bias}
        </span>
      </article>

      <article className="gpu-capital-card">
        <span className="gpu-metric-key">MILESTONE METRIC</span>
        <strong className="gpu-capital-value gpu-capital-value--sm">
          £{Math.abs(capital.realizedTodayGbp).toFixed(2)} / £
          {capital.milestoneGbp.toLocaleString("en-GB", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}
        </strong>
        <div className="gpu-milestone-track" aria-hidden>
          <div
            className="gpu-milestone-fill"
            style={{ width: `${Math.min(100, Math.max(0, capital.progressPct))}%` }}
          />
        </div>
        <span className="gpu-capital-sub">
          {capital.progressPct.toFixed(1)}% of £1k daily · journal pipeline
        </span>
      </article>

      <article className="gpu-capital-card">
        <span className="gpu-metric-key">NET INTRA-DAY CASH FLOW</span>
        <strong className={`gpu-capital-value ${toneClass(capital.netCashFlowGbp)}`}>
          {fmtGbp(capital.netCashFlowGbp)}
        </strong>
        <span className="gpu-capital-sub">realized + open UPL equity shift</span>
      </article>

      <article className="gpu-capital-card">
        <span className="gpu-metric-key">EXECUTION WIN RATE &amp; PF</span>
        <strong className="gpu-capital-value gpu-capital-value--sm">
          {wr} · PF {pf}
        </strong>
        <span className="gpu-capital-sub">
          {capital.wins}W / {capital.losses}L · backend history
        </span>
      </article>
    </section>
  );
}
