/**
 * Full-screen warning when IPC stream drops or goes stale.
 * @param {{ visible: boolean, transport?: string }} props
 */
export default function NetworkDegradedOverlay({ visible, transport = "apex-ipc" }) {
  if (!visible) return null;
  return (
    <div className="apex-network-degraded" role="alert" aria-live="assertive">
      <div className="apex-network-degraded-inner">
        <span className="apex-network-degraded-title">NETWORK DEGRADED</span>
        <span className="apex-network-degraded-sub apex-hud-subtitle">
          {transport} tick stream interrupted — awaiting reconnection to apex_ipc.sock
        </span>
      </div>
    </div>
  );
}
