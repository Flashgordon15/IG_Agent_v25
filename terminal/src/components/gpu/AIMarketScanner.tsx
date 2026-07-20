"use client";

import { QuantumRouterAuditModal } from "@/components/QuantumRouterAuditModal";
import type { SniperMarketRow } from "@/lib/quantum-node-types";

type Props = {
  rows: SniperMarketRow[];
  wsState: string;
};

/** Desk sniper arm threshold — below = chop isolation (slate); at/above = emerald flash */
const SNIPER_GATE = 0.68;

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

function RegimeScoreCell({ conviction }: { conviction: number | null }) {
  const pct =
    conviction != null && Number.isFinite(conviction)
      ? Math.round(Math.max(0, Math.min(1, conviction)) * 100)
      : null;
  const armed = pct != null && pct >= Math.round(SNIPER_GATE * 100);

  return (
    <div
      className={`sniper-conviction ${armed ? "sniper-conviction--armed" : "sniper-conviction--slate"}`}
    >
      <strong className="gpu-ledger-mono sniper-regime-score">
        {pct != null ? `AI Matrix Regime Score ${pct}%` : "AI Matrix Regime Score —"}
      </strong>
      <span className="sniper-regime-status">
        {armed
          ? "[🚨 SNIPER THRESHOLD COMPLIANT]"
          : "[STATUS: BELOW SNIPER GATE — SEE AI WHY IDLE]"}
      </span>
      <div className="gpu-milestone-track" aria-hidden>
        <div
          className="gpu-milestone-fill"
          style={{
            width: `${Math.min(100, Math.max(0, pct ?? 0))}%`,
          }}
        />
      </div>
    </div>
  );
}

export function AIMarketScanner({ rows, wsState }: Props) {
  return (
    <section className="gpu-fleet-panel sniper-hub" aria-label="AI multi-market scanner">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Multi-Market AI Strategy Hub</p>
          <h2 className="gpu-panel-title">AI Market Scanner</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip gpu-chip--live">WS {wsState.toUpperCase()}</span>
          <span className="gpu-chip">DOW · DAX · GOLD · BRENT</span>
          <span className="gpu-chip">GATE {(SNIPER_GATE * 100).toFixed(0)}%</span>
          <QuantumRouterAuditModal triggerClassName="blueprint-trigger blueprint-trigger--router" />
        </div>
      </header>

      <div className="sniper-matrix" role="table">
        <div className="sniper-matrix-head" role="row">
          <span>ASSET</span>
          <span>MID</span>
          <span>AI MATRIX REGIME</span>
          <span>STRATEGY STATUS</span>
        </div>
        {rows.map((row) => (
          <div key={row.id} className="sniper-row" role="row">
            <div className="sniper-asset">
              <strong>{row.label}</strong>
              <span className="sniper-epic">{row.epic}</span>
              <span className="sniper-meta">
                {row.tpm.toFixed(0)} tpm · z {row.zScore.toFixed(2)} · {row.regimeLabel}
              </span>
            </div>
            <div className="sniper-mid">
              <strong className="gpu-ledger-mono">{fmtMid(row)}</strong>
              <span className="sniper-meta">{row.profile}</span>
            </div>
            <RegimeScoreCell conviction={row.conviction} />
            <div className={statusClass(row.statusKind)}>{row.statusText}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
