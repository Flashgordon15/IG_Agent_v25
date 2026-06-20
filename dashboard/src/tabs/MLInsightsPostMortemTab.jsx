import { memo, useMemo, useState } from "react";
import { useApexTelemetry } from "../apex/useApexTelemetry.js";
import AllocationPieChart from "../components/apex/AllocationPieChart.jsx";

const MOCK_SEGMENTS = [
  { label: "Wins", value: 70, color: "#2ec4b6" },
  { label: "Losses", value: 30, color: "#ff9f1c" },
];

const MOCK_FEATURE_WEIGHTS = [
  { feature: "Volume Momentum", weight_pct: 85 },
  { feature: "Spread Z-Score", weight_pct: 72 },
  { feature: "Session Bias", weight_pct: 64 },
  { feature: "ATR Regime", weight_pct: 58 },
  { feature: "RSI Divergence", weight_pct: 41 },
];

/**
 * Standalone ML Insights & Post-Mortem diagnostics panel.
 */
function MLInsightsPostMortemTab() {
  const { telemetry } = useApexTelemetry();
  const ml = telemetry?.transparency?.ml_post_mortem ?? null;
  const [selectedId, setSelectedId] = useState(null);

  const segments = useMemo(() => {
    if (ml) {
      const wins = Number(ml.win_pct) || 0;
      const losses = Number(ml.loss_pct) || 0;
      if (wins > 0 || losses > 0) {
        return [
          { label: "Wins", value: wins, color: "#2ec4b6" },
          { label: "Losses", value: losses, color: "#ff9f1c" },
        ];
      }
    }
    return MOCK_SEGMENTS;
  }, [ml]);

  const losingTrades = ml?.losing_trades ?? [];
  const useMockAutopsy = losingTrades.length === 0;
  const selected = losingTrades.find((t) => t.deal_id === selectedId) ?? null;

  return (
    <div className="apex-ml-tab">
      <header className="apex-ml-tab__header">
        <h1>ML Insights &amp; Post-Mortem</h1>
        <p className="apex-ml-tab__sub">
          Closed transaction ledger · feature-weight autopsy · anomaly flags
        </p>
      </header>

      <div className="apex-ml-tab__grid">
        <div className="apex-ml-tab__chart-card">
          <h2>Performance Split</h2>
          <AllocationPieChart segments={segments} />
          <div className="apex-ml-tab__split-stats">
            <span className="apex-ml-tab__win">{segments[0]?.value ?? 70}% Wins</span>
            <span className="apex-ml-tab__loss">{segments[1]?.value ?? 30}% Losses</span>
            <span className="apex-ml-tab__total">
              {useMockAutopsy ? "Mock simulation" : `${ml?.total_closed ?? 0} closed`}
            </span>
          </div>
        </div>

        <div className="apex-ml-tab__autopsy">
          <h2>Historical Feature-Weight Autopsy Grid</h2>
          {useMockAutopsy ? (
            <div className="apex-mock-autopsy-grid">
              {MOCK_FEATURE_WEIGHTS.map((fw) => (
                <div key={fw.feature} className="apex-mock-autopsy-row">
                  <span>
                    {fw.feature}: {fw.weight_pct}%
                  </span>
                  <div className="apex-mock-autopsy-meter">
                    <div style={{ width: `${fw.weight_pct}%` }} />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <>
              <ul className="apex-ml-tab__loss-list">
                {losingTrades.map((trade) => (
                  <li key={trade.deal_id || trade.entry_time}>
                    <button
                      type="button"
                      className={`apex-ml-tab__loss-btn ${selectedId === trade.deal_id ? "apex-ml-tab__loss-btn--active" : ""}`}
                      onClick={() => setSelectedId(trade.deal_id)}
                    >
                      <span className="apex-ml-tab__loss-instrument">{trade.instrument}</span>
                      <span className="apex-ml-tab__loss-pnl">£{Number(trade.gbp_pnl).toFixed(2)}</span>
                      <span className="apex-ml-tab__loss-time">{trade.exit_time || trade.entry_time}</span>
                      {trade.anomaly && (
                        <span className="apex-ml-tab__anomaly-tag">ANOMALY</span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>

              {selected && (
                <div className="apex-ml-tab__detail">
                  <div className="apex-ml-tab__detail-head">
                    <h3>{selected.instrument}</h3>
                    <p>
                      Model Confidence:{" "}
                      <strong>{selected.model_confidence_pct ?? "—"}%</strong>
                    </p>
                    {selected.anomaly && (
                      <p className="apex-ml-tab__anomaly-reason">
                        Anomaly Flag: {selected.anomaly_reason || "Unprecedented market event"}
                      </p>
                    )}
                  </div>
                  <table className="apex-ml-tab__weights">
                    <thead>
                      <tr>
                        <th>Feature Weights</th>
                        <th>Influence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(selected.feature_weights ?? []).map((fw) => (
                        <tr key={fw.feature}>
                          <td>{fw.feature}</td>
                          <td>
                            <div className="apex-ml-tab__meter-track">
                              <div
                                className="apex-ml-tab__meter-fill"
                                style={{ width: `${Math.min(100, fw.weight_pct)}%` }}
                              />
                              <span>{fw.weight_pct}%</span>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default memo(MLInsightsPostMortemTab);
