import { memo, useMemo } from "react";
import { useApexTelemetry } from "../../apex/useApexTelemetry.js";
import { useSidecarPid } from "../../hooks/useSidecarPid.js";
import ApexWebGLCanvas from "./ApexWebGLCanvas.jsx";
import AvionicsHudCard from "./AvionicsHudCard.jsx";
import AvionicsStorytellingTerminal from "./AvionicsStorytellingTerminal.jsx";
import PillarScorecardCard from "./PillarScorecardCard.jsx";
import LiveTradesLedger from "./LiveTradesLedger.jsx";
import OperationFilteringFunnel from "./OperationFilteringFunnel.jsx";
import SystemHealthGridPanel from "./SystemHealthGridPanel.jsx";
import NetworkDegradedOverlay from "./NetworkDegradedOverlay.jsx";
import { PRIMARY_RING_KEYS, ASSET_LABELS } from "../../apex/constants.js";

const MemoFunnel = memo(OperationFilteringFunnel);
const MemoHealthGrid = memo(SystemHealthGridPanel);
const MemoStoryTerminal = memo(AvionicsStorytellingTerminal);
const MemoWebGL = memo(ApexWebGLCanvas);
const MemoHudCard = memo(AvionicsHudCard);
const MemoPillar = memo(PillarScorecardCard);
const MemoLedger = memo(LiveTradesLedger);

/**
 * Project Apex — unified avionics cockpit (WebGL + storytelling + ledger).
 */
function ApexCockpitView() {
  const { telemetry, ipcConnected, networkDegraded, transport, tickCount, isDesktop } =
    useApexTelemetry();
  const sidecarPid = useSidecarPid(telemetry?.tick?.agent_pid);

  const transparency = useMemo(
    () => telemetry?.transparency ?? null,
    [telemetry?.transparency],
  );
  const assets = useMemo(() => telemetry?.assets ?? {}, [telemetry?.assets]);
  const pillars = useMemo(() => telemetry?.pillars ?? null, [telemetry?.pillars]);

  return (
    <div className="apex-cockpit-shell">
      <NetworkDegradedOverlay visible={networkDegraded} transport={transport} />

      <header className="apex-cockpit-header">
        <div>
          <h1 className="apex-cockpit-title">Project Apex Monolith</h1>
          <p className="apex-cockpit-sub">
            Midnight Indigo HUD · PID {sidecarPid ?? "—"} · {ASSET_LABELS.GOLD} /{" "}
            {ASSET_LABELS.WALL_STREET} ·{" "}
            {isDesktop ? "Electron UDS IPC" : "HTTP poll"}
          </p>
        </div>
        <div className="apex-cockpit-status">
          <span
            className={`apex-pill ipc-status ${ipcConnected ? "apex-pill-ok" : "apex-pill-warn"}`}
          >
            IPC {ipcConnected ? "LIVE" : "DOWN"}
          </span>
          <span className="apex-pill">{transport}</span>
          <span className="apex-pill">ticks {tickCount}</span>
        </div>
      </header>

      <MemoStoryTerminal telemetry={telemetry} />

      <div className="apex-transparency-row">
        <MemoFunnel transparency={transparency} />
        <MemoHealthGrid transparency={transparency} />
      </div>

      <div className="apex-viewport-wrap">
        <div className="apex-webgl-stage">
          <div className="apex-webgl-label apex-webgl-label--gold">GC=F · GOLD</div>
          <div className="apex-webgl-label apex-webgl-label--wall">^DJI · WALL ST</div>
          <MemoWebGL telemetry={telemetry} className="apex-webgl-canvas" />
        </div>
        <div className="apex-viewport-grid">
          <MemoHudCard assets={assets} focusKeys={PRIMARY_RING_KEYS} />
          <MemoPillar pillars={pillars} />
        </div>
      </div>

      <MemoLedger />
    </div>
  );
}

export default memo(ApexCockpitView);
