import { ASSET_KEYS, ASSET_LABELS } from "../../apex/constants.js";
import { fmtPrice } from "../../utils/fmtPrice.js";

function pct(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(1)}%`;
}

const RING_NEON = {
  GOLD: { hi: "#ff9f1c", lo: "#e63946" },
  WALL_STREET: { hi: "#00b4d8", lo: "#7209b7" },
  DEFAULT: { hi: "#2ec4b6", lo: "#6b7c96" },
};

function ringStyle(confidence, assetKey) {
  const pctVal = confidence != null ? Math.min(100, Math.max(0, confidence)) : 0;
  const deg = (pctVal / 100) * 360;
  const neon = RING_NEON[assetKey] ?? RING_NEON.DEFAULT;
  const color = pctVal >= 42 ? neon.hi : neon.lo;
  return {
    background: `conic-gradient(${color} ${deg}deg, rgba(28,35,74,0.9) ${deg}deg)`,
    boxShadow: pctVal >= 42 ? `0 0 14px ${color}55` : "none",
  };
}

/**
 * Card B — Avionics HUD telemetry rings (HTML overlay on WebGL viewport).
 * @param {{ assets: Record<string, import('../../apex/types.js').AvionicsAssetTelemetry>, focusKeys?: string[] }} props
 */
export default function AvionicsHudCard({ assets, focusKeys }) {
  const keys = focusKeys?.length ? focusKeys : ASSET_KEYS;
  return (
    <section className="apex-card apex-card-b" aria-label="Card B Avionics Telemetry HUD">
      <header className="apex-card-header">
        <span className="apex-card-label">Card B</span>
        <h2>Avionics Telemetry HUD</h2>
        <span className="apex-badge">Threshold Walls · 45% / 55%</span>
      </header>
      <div className="apex-asset-grid">
        {keys.map((key) => {
          const row = assets?.[key] ?? {};
          const epic = row.epic || key;
          return (
            <article key={key} className="apex-asset-tile" data-asset={key}>
              <div className="apex-ring-wrap">
                <div className="apex-ring" style={ringStyle(row.confidence, key)}>
                  <div className="apex-ring-core">
                    <span className="apex-ring-price">
                      {row.mid != null ? fmtPrice(row.mid, epic) : "—"}
                    </span>
                  </div>
                </div>
              </div>
              <div className="apex-asset-meta">
                <span className="apex-asset-name">{ASSET_LABELS[key]}</span>
                <span className="apex-asset-dir">{row.direction || "WAIT"}</span>
                <div className="apex-asset-stats">
                  <span>Conf {pct(row.confidence)}</span>
                  <span>Fit {row.fitness != null ? pct(row.fitness <= 1 ? row.fitness * 100 : row.fitness) : "—"}</span>
                  <span>RSI {row.rsi != null ? row.rsi.toFixed(1) : "—"}</span>
                </div>
                <span className="apex-asset-stream">{row.streamStatus || "—"}</span>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
