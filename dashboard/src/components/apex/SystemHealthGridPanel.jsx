import { useEffect, useMemo, useRef, useState } from "react";
import { subscribeStory } from "../../api/apexIpc.js";

/**
 * System role health matrix + API heartbeat + micro-action ticker.
 * @param {{ transparency: import('../../apex/types.js').OperationalTransparency | null }} props
 */
export default function SystemHealthGridPanel({ transparency }) {
  const [storyLines, setStoryLines] = useState([]);
  const tickerRef = useRef(null);

  useEffect(() => {
    return subscribeStory((packet) => {
      if (!packet || packet.kind !== "micro_action") return;
      setStoryLines((prev) => {
        const line = `[${packet.ts_utc || "—"} UTC] ${packet.line || ""}`;
        return [line, ...prev].slice(0, 40);
      });
    });
  }, []);

  const health = transparency?.health_grid ?? null;
  const roles = health?.roles ?? [];
  const bridgeDetail = health?.bridge_detail ?? "Awaiting handshake";
  const microTicker = useMemo(() => {
    const backend = transparency?.micro_ticker ?? [];
    const fromBackend = backend.map(
      (row) => `[${row.ts_utc || "—"} UTC] ${row.line || ""}`,
    );
    const merged = [...storyLines, ...fromBackend];
    const seen = new Set();
    return merged.filter((line) => {
      if (seen.has(line)) return false;
      seen.add(line);
      return true;
    }).slice(0, 24);
  }, [transparency, storyLines]);

  useEffect(() => {
    const el = tickerRef.current;
    if (!el) return;
    el.scrollTop = 0;
  }, [microTicker.length]);

  return (
    <section className="apex-health-grid" aria-label="System Health Grid">
      <header className="apex-health-grid__header">
        <h2>System Health Grid</h2>
        <span className="apex-health-grid__tag">Pillar 1 &amp; 4</span>
        {health?.recovery_badge ? (
          <span
            className="apex-health-grid__recovery-badge"
            title="Watchdog self-healer staging reclamation"
          >
            {health.recovery_badge}
          </span>
        ) : null}
      </header>

      <div className="apex-health-grid__bridge">
        <span className="apex-health-grid__bridge-label">
          {health?.bridge_label || "Core Communications Bridge"}
        </span>
        <span className="apex-health-grid__bridge-value">{bridgeDetail}</span>
      </div>

      <ul className="apex-health-grid__roles">
        {roles.length === 0 ? (
          <li className="apex-health-grid__role apex-health-grid__role--idle">
            <span className="apex-health-grid__role-label">Awaiting role telemetry…</span>
          </li>
        ) : (
          roles.map((role) => (
            <li
              key={role.id}
              className={`apex-health-grid__role apex-health-grid__role--${role.status}`}
            >
              <span
                className={`apex-health-grid__badge apex-health-grid__badge--${role.status}`}
                aria-hidden="true"
              />
              <div className="apex-health-grid__role-body">
                <span className="apex-health-grid__role-label">
                  {role.label}: {role.status === "active" ? "ACTIVE" : "FAILED"}
                </span>
                <span className="apex-health-grid__role-detail">{role.detail}</span>
              </div>
            </li>
          ))
        )}
      </ul>

      <div className="apex-health-grid__ticker-wrap">
        <div className="apex-health-grid__ticker-label">Live Micro-Action Ticker</div>
        <div className="apex-health-grid__ticker" ref={tickerRef} role="log" aria-live="polite">
          {microTicker.length === 0 ? (
            <p className="apex-health-grid__ticker-line apex-health-grid__ticker-line--idle">
              [— UTC] Passive background roles idle — awaiting first broker evaluation…
            </p>
          ) : (
            microTicker.map((line, i) => (
              <p key={`${line}-${i}`} className="apex-health-grid__ticker-line">
                {line}
              </p>
            ))
          )}
        </div>
      </div>
    </section>
  );
}
