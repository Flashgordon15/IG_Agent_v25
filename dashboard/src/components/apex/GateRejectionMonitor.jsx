import { useMemo } from "react";
import { extractGateRejections } from "../../apex/gateRejectionParser.js";
import { ASSET_LABELS } from "../../apex/constants.js";

/**
 * CORE LIFE-CYCLE GATE REJECTION MONITOR — streams hold tokens from entry gates.
 * @param {{ telemetry: import('../../apex/types.js').ParsedApexTelemetry | null }} props
 */
export default function GateRejectionMonitor({ telemetry }) {
  const rejections = useMemo(() => extractGateRejections(telemetry), [telemetry]);
  const allClear = rejections.length === 0;

  return (
    <section
      className="apex-gate-monitor"
      aria-label="CORE LIFE-CYCLE GATE REJECTION MONITOR"
    >
      <header className="apex-gate-monitor__header">
        <h2>CORE LIFE-CYCLE GATE REJECTION MONITOR</h2>
        <span className={`apex-gate-monitor__badge ${allClear ? "apex-gate-monitor__badge--ok" : ""}`}>
          {allClear ? "ALL GATES CLEAR" : `${rejections.length} ACTIVE VETO${rejections.length === 1 ? "" : "S"}`}
        </span>
      </header>
      <div className="apex-gate-monitor__terminal" role="log" aria-live="polite">
        {allClear ? (
          <p className="apex-gate-monitor__line apex-gate-monitor__line--ok">
            PASS — no lifecycle gate rejections on Gold / Wall St tracks
          </p>
        ) : (
          rejections.map((row, i) => (
            <p key={`${row.asset}-${row.line}-${i}`} className="apex-gate-monitor__line apex-gate-monitor__line--hold">
              <span className="apex-gate-monitor__asset">{ASSET_LABELS[row.asset] || row.asset}</span>
              {row.line}
            </p>
          ))
        )}
      </div>
    </section>
  );
}
