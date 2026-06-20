import { useEffect, useMemo, useRef, useState } from "react";
import { subscribeLedger } from "../../api/apexIpc.js";
import { ASSET_LABELS, PRIMARY_RING_KEYS } from "../../apex/constants.js";
import { fmtPrice } from "../../utils/fmtPrice.js";
import AllocationPieChart from "./AllocationPieChart.jsx";
import VolatilityWaveChart from "./VolatilityWaveChart.jsx";

const PIE_COLORS = {
  GOLD: "#ff9f1c",
  WALL_STREET: "#00b4d8",
  JAPAN_225: "#7209b7",
  EUR_USD: "#2ec4b6",
};

const WAVE_CAP = 64;

/**
 * Institutional midnight HUD — asset summary matrix, pie allocation, volatility wave.
 * @param {{ telemetry: import('../../apex/types.js').ParsedApexTelemetry | null }} props
 */
export default function AvionicsStorytellingTerminal({ telemetry }) {
  const [trades, setTrades] = useState([]);
  const waveHistory = useRef([]);

  useEffect(() => {
    return subscribeLedger((packet) => {
      if (!packet || typeof packet !== "object") return;
      const latency = packet.latency_ms ?? packet.latencyMs;
      setTrades((prev) => [
        {
          id: `${packet.deal_id || packet.ts}-${Date.now()}`,
          ts: packet.ts_iso || packet.ts,
          epic: String(packet.epic || "—"),
          action: String(packet.action || packet.side || "—").toUpperCase(),
          size: packet.size != null ? Math.trunc(Number(packet.size)) : 0,
          entry: packet.entry ?? packet.entry_price,
          latencyMs: latency,
        },
        ...prev,
      ].slice(0, 50));
    });
  }, []);

  const metrics = useMemo(() => buildMetrics(telemetry, trades), [telemetry, trades]);

  const pieSegments = useMemo(() => {
    const assets = telemetry?.assets ?? {};
    return PRIMARY_RING_KEYS.map((key) => ({
      label: ASSET_LABELS[key] ?? key,
      value: Math.max(0, Number(assets[key]?.confidence ?? 0)),
      color: PIE_COLORS[key] ?? "#6b7c96",
    }));
  }, [telemetry?.assets]);

  const waveSeries = useMemo(() => {
    const gold = telemetry?.assets?.GOLD?.mid;
    if (gold != null && Number.isFinite(Number(gold))) {
      const buf = waveHistory.current;
      buf.push(Number(gold));
      if (buf.length > WAVE_CAP) buf.shift();
    }
    return [...waveHistory.current];
  }, [telemetry?.assets?.GOLD?.mid, telemetry?.tick?.ts]);

  return (
    <section className="apex-data-dashboard" aria-label="Apex institutional data summary">
      <header className="apex-data-dashboard__header">
        <h2 className="apex-data-dashboard__title">Institutional Telemetry Matrix</h2>
        <span className="apex-data-dashboard__sub">
          Live asset summary · activity mix · volatility wave
        </span>
      </header>

      <div className="apex-data-grid">
        <div className="apex-data-col apex-data-col--summary">
          <h3 className="apex-data-col__heading">Asset Summary List</h3>
          <ul className="apex-summary-list">
            {metrics.rows.map((row) => (
              <li key={row.label} className="apex-summary-row">
                <span className="apex-summary-label">{row.label}</span>
                <span
                  className={`apex-summary-value ${row.positive ? "apex-summary-value--neon" : ""}`}
                >
                  {row.value}
                </span>
              </li>
            ))}
          </ul>

          <div className="apex-ticker-strip">
            <span className="apex-summary-label">Last Trade Sync Tickers</span>
            <div className="apex-ticker-chips">
              {metrics.lastTickers.length ? (
                metrics.lastTickers.map((t) => (
                  <span key={t} className="apex-ticker-chip">
                    {t}
                  </span>
                ))
              ) : (
                <span className="apex-summary-label">— awaiting fill</span>
              )}
            </div>
          </div>
        </div>

        <div className="apex-data-col apex-data-col--charts">
          <AllocationPieChart segments={pieSegments} />
          <VolatilityWaveChart series={waveSeries} height={128} />
        </div>
      </div>
    </section>
  );
}

function buildMetrics(telemetry, trades) {
  const txCount = trades.length;
  const volume = trades.reduce((s, t) => s + (Number(t.size) || 0), 0);
  const envelopePct = telemetry?.pillars?.envelopeUtilPct ?? 0;
  const assets = telemetry?.assets ?? {};
  const confidences = PRIMARY_RING_KEYS.map((k) => Number(assets[k]?.confidence ?? 0)).filter(
    (c) => c > 0,
  );
  const analyzedRatio =
    confidences.length > 0
      ? confidences.reduce((a, b) => a + b, 0) / confidences.length
      : 0;

  const lastTickers = [
    ...new Set(
      trades
        .slice(0, 5)
        .map((t) => t.epic)
        .filter(Boolean),
    ),
  ];

  const lastTrade = trades[0];
  const lastEntry =
    lastTrade?.entry != null ? fmtPrice(lastTrade.entry, lastTrade.epic) : "—";

  return {
    rows: [
      { label: "Transactions", value: String(txCount), positive: txCount > 0 },
      { label: "Volume (lots)", value: String(volume), positive: volume > 0 },
      {
        label: "Investment Use Rate",
        value: `${envelopePct.toFixed(1)}%`,
        positive: envelopePct > 0 && envelopePct < 85,
      },
      {
        label: "Analyzed Ratio",
        value: `${analyzedRatio.toFixed(1)}%`,
        positive: analyzedRatio >= 42,
      },
      { label: "Last Fill Entry", value: lastEntry, positive: lastTrade != null },
    ],
    lastTickers,
  };
}
