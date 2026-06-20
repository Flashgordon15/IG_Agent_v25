import { useMemo } from "react";

const EMPTY_FUNNEL = {
  opportunities_scanned: 0,
  spread_rejections: 0,
  liquidity_blocks: 0,
  ml_veto_flags: 0,
  weekend_blackout_holds: 0,
  executed_trades: 0,
};

/**
 * Three-tier operational funnel — scanned → rejected → executed.
 * @param {{ transparency: import('../../apex/types.js').OperationalTransparency | null }} props
 */
export default function OperationFilteringFunnel({ transparency }) {
  const funnel = useMemo(() => {
    const raw = transparency?.funnel;
    if (!raw || typeof raw !== "object") return { ...EMPTY_FUNNEL };
    return { ...EMPTY_FUNNEL, ...raw };
  }, [transparency]);

  const rejectTotal =
    funnel.spread_rejections +
    funnel.liquidity_blocks +
    funnel.ml_veto_flags +
    funnel.weekend_blackout_holds;

  const tiers = [
    {
      id: "scan",
      label: "OPPORTUNITIES SCANNED",
      value: funnel.opportunities_scanned,
      sub: "Parallel 4-worker NumPy ring buffers",
    },
    {
      id: "reject",
      label: "REJECTION MATRIX FILTER",
      value: rejectTotal,
      sub: "12 institutional risk gates",
      breakdown: [
        { label: "Spread Rejections", value: funnel.spread_rejections },
        { label: "Liquidity Blocks (ATR < 20)", value: funnel.liquidity_blocks },
        { label: "ML Veto Flags", value: funnel.ml_veto_flags },
        { label: "Weekend Blackout Holds", value: funnel.weekend_blackout_holds },
      ],
    },
    {
      id: "exec",
      label: "EXECUTED TRADES",
      value: funnel.executed_trades,
      sub: "LiveExecutor → IG DEMO REST",
    },
  ];

  const maxVal = Math.max(
    funnel.opportunities_scanned,
    rejectTotal,
    funnel.executed_trades,
    1,
  );

  return (
    <section className="apex-funnel" aria-label="Operation Filtering Funnel">
      <header className="apex-funnel__header">
        <h2>Operation Filtering Funnel</h2>
        <span className="apex-funnel__tag">Pillar 3 &amp; 5</span>
      </header>
      <div className="apex-funnel__tiers">
        {tiers.map((tier, idx) => (
          <div key={tier.id} className="apex-funnel__tier">
            {idx > 0 && <div className="apex-funnel__connector" aria-hidden="true" />}
            <div className="apex-funnel__tier-head">
              <span className="apex-funnel__tier-label">{tier.label}</span>
              <span className="apex-funnel__tier-value">{tier.value.toLocaleString()}</span>
            </div>
            <div className="apex-funnel__bar-track">
              <div
                className={`apex-funnel__bar apex-funnel__bar--${tier.id}`}
                style={{ width: `${Math.max(4, (tier.value / maxVal) * 100)}%` }}
              />
            </div>
            <p className="apex-funnel__tier-sub">{tier.sub}</p>
            {tier.breakdown && (
              <ul className="apex-funnel__breakdown">
                {tier.breakdown.map((row) => (
                  <li key={row.label}>
                    <span>{row.label}</span>
                    <strong>{row.value.toLocaleString()}</strong>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
