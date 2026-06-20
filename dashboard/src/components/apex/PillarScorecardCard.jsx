function fmtGbp(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return `£${Number(n).toLocaleString("en-GB", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

function statusClass(status) {
  if (status === "active") return "apex-pillar-active";
  if (status === "degraded") return "apex-pillar-degraded";
  return "apex-pillar-blocked";
}

/**
 * Card D — Pillar scorecard pool matrix overlay.
 * @param {{ pillars: import('../../apex/types.js').PillarTelemetry | null }} props
 */
export default function PillarScorecardCard({ pillars }) {
  if (!pillars) {
    return (
      <section className="apex-card apex-card-d" aria-label="Card D Pillar Scorecard">
        <header className="apex-card-header">
          <span className="apex-card-label">Card D</span>
          <h2>Pillar Scorecard</h2>
        </header>
        <p className="apex-muted">Awaiting telemetry stream…</p>
      </section>
    );
  }

  const utilPct = Math.min(100, pillars.envelopeUtilPct);

  return (
    <section className="apex-card apex-card-d" aria-label="Card D Pillar Scorecard Pool Matrix">
      <header className="apex-card-header">
        <span className="apex-card-label">Card D</span>
        <h2>Pillar Scorecard</h2>
        <span className="apex-badge">Pool Matrix</span>
      </header>

      <div className="apex-envelope-grid">
        <div className="apex-envelope-stat">
          <span className="apex-stat-label">Real-Money Baseline</span>
          <span className="apex-stat-value">{fmtGbp(pillars.baselineEquityGbp)}</span>
        </div>
        <div className="apex-envelope-stat">
          <span className="apex-stat-label">Portfolio Envelope</span>
          <span className="apex-stat-value">{fmtGbp(pillars.portfolioEnvelopeGbp)}</span>
        </div>
        <div className="apex-envelope-stat">
          <span className="apex-stat-label">Concurrent Allocation</span>
          <span className="apex-stat-value">{fmtGbp(pillars.concurrentRiskGbp)}</span>
          <div className="apex-util-track">
            <div className="apex-util-fill" style={{ width: `${utilPct}%` }} />
          </div>
        </div>
        <div className="apex-envelope-stat">
          <span className="apex-stat-label">ML Veto Floor</span>
          <span className={`apex-stat-value ${pillars.mlUnblocked ? "text-success" : "text-warning"}`}>
            {pillars.mlVetoFloor.toFixed(3)}
            {pillars.mlUnblocked ? " · UNBLOCKED" : " · VETO"}
          </span>
          {pillars.mlProbability != null && (
            <span className="apex-stat-sub">
              p={pillars.mlProbability.toFixed(3)}
            </span>
          )}
        </div>
      </div>

      <div className="apex-pillar-matrix">
        {pillars.pillars.map((p) => (
          <div key={p.id} className={`apex-pillar-row ${statusClass(p.status)}`}>
            <span className="apex-pillar-id">P{p.id}</span>
            <span className="apex-pillar-label">{p.label}</span>
            <span className="apex-pillar-detail">{p.detail}</span>
            <span className="apex-pillar-status">{p.status.toUpperCase()}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
