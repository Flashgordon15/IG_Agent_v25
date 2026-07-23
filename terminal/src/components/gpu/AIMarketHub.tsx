"use client";

import type { SniperMarketRow } from "@/lib/quantum-node-types";

type Props = {
  rows: SniperMarketRow[];
  wsState: string;
};

function fmtMid(row: SniperMarketRow): string {
  if (!(row.mid > 0)) return "—";
  if (row.id === "brent" || row.id === "gold") return row.mid.toFixed(2);
  return row.mid.toFixed(1);
}

function statusClass(kind: SniperMarketRow["statusKind"]): string {
  if (kind === "long") return "sniper-flag sniper-flag--long";
  if (kind === "short") return "sniper-flag sniper-flag--short";
  return "sniper-flag sniper-flag--proxy";
}

function MiniSpark({ ticks, kind }: { ticks: number[]; kind: string }) {
  const w = 120;
  const h = 28;
  if (ticks.length < 2) {
    return (
      <svg className="sniper-spark" viewBox={`0 0 ${w} ${h}`} aria-hidden>
        <rect width={w} height={h} fill="transparent" />
      </svg>
    );
  }
  const min = Math.min(...ticks);
  const max = Math.max(...ticks);
  const span = Math.max(1e-9, max - min);
  const pts = ticks
    .map((v, i) => {
      const x = (i / (ticks.length - 1)) * (w - 2) + 1;
      const y = 1 + (1 - (v - min) / span) * (h - 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const stroke =
    kind === "long"
      ? "var(--gpu-emerald)"
      : kind === "short"
        ? "var(--gpu-cyan, #60a5fa)"
        : "var(--gpu-amber)";
  return (
    <svg className="sniper-spark" viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth="1.4"
        strokeLinejoin="round"
        points={pts}
      />
    </svg>
  );
}

export function AIMarketHub({ rows, wsState }: Props) {
  return (
    <section className="gpu-fleet-panel sniper-hub" aria-label="AI multi-market scanner">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Asynchronous AI Multi-Market Scanner</p>
          <h2 className="gpu-panel-title">AI Market Hub</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip gpu-chip--live">WS {wsState.toUpperCase()}</span>
          <span className="gpu-chip">{rows.length} ASSETS</span>
        </div>
      </header>

      <div className="sniper-matrix" role="table">
        <div className="sniper-matrix-head" role="row">
          <span>ASSET</span>
          <span>MID</span>
          <span>STRATEGY PROFILE</span>
          <span>DIRECTIONAL STATUS</span>
        </div>
        {rows.map((row) => (
          <div key={row.id} className="sniper-row" role="row">
            <div className="sniper-asset">
              <strong>{row.label}</strong>
              <span className="sniper-epic">{row.epic}</span>
              <MiniSpark ticks={row.ticks} kind={row.statusKind} />
            </div>
            <div className="sniper-mid">
              <strong className="gpu-ledger-mono">{fmtMid(row)}</strong>
              <span className="sniper-meta">
                {row.tpm.toFixed(0)} tpm · z {row.zScore.toFixed(2)}
              </span>
            </div>
            <div className="sniper-profile">
              <span className="sniper-profile-main">{row.profile}</span>
              <span className="sniper-meta">
                {row.regimeLabel}
                {row.inActiveStack ? " · ACTIVE STACK" : " · ELIGIBLE/IDLE"}
                {row.allowEntries ? " · ENTRIES ON" : " · ENTRIES GATED"}
              </span>
            </div>
            <div className={statusClass(row.statusKind)}>{row.statusText}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
