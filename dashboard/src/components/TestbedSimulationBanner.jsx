import { memo, useCallback, useEffect, useState } from "react";
import { api } from "../api/client.js";

const SPEEDS = [
  { label: "1x Speed", value: 1 },
  { label: "10x Speed", value: 10 },
  { label: "100x Hyper-Drive", value: 100 },
];

function TestbedSimulationBanner({ onActiveChange }) {
  const [status, setStatus] = useState(null);
  const [busySpeed, setBusySpeed] = useState(null);
  const [error, setError] = useState("");

  const poll = useCallback(async () => {
    try {
      const data = await api.testbedStatus();
      setStatus(data);
      setError("");
      onActiveChange?.(Boolean(data?.hardened_testbed));
    } catch (e) {
      setError("");
      onActiveChange?.(false);
    }
  }, [onActiveChange]);

  useEffect(() => {
    poll();
    const id = window.setInterval(poll, 100);
    return () => window.clearInterval(id);
  }, [poll]);

  const setSpeed = async (speed) => {
    setBusySpeed(speed);
    try {
      const data = await api.testbedSetReplaySpeed(speed);
      setStatus(data);
      setError("");
    } catch (e) {
      setError(e.message || "Speed change failed");
    } finally {
      setBusySpeed(null);
    }
  };

  if (!status?.hardened_testbed) {
    return null;
  }

  const winPct = status.win_rate_pct;
  const activeSpeed = Number(status.replay_speed ?? 100);

  return (
    <div className="testbed-sim-banner" role="status" aria-live="polite">
      <div className="testbed-sim-banner__head">
        <span className="testbed-sim-banner__title">
          ⚠️ HARDENED TESTBED SIMULATION ACTIVE
        </span>
        <div className="testbed-sim-banner__metrics">
          <span className="testbed-sim-metric">
            Virtual Time: <strong>{status.virtual_time_hms ?? "—"}</strong>
          </span>
          <span className="testbed-sim-metric">
            Ingestion Speed: <strong>{status.ticks_per_sec ?? 0} Ticks/Sec</strong>
          </span>
          <span className="testbed-sim-metric">
            Archive Progress: <strong>{status.total_ticks?.toLocaleString() ?? 0}</strong>
          </span>
          {winPct != null && (
            <span className="testbed-sim-metric testbed-sim-winrate">
              Win Rate: <strong>{winPct}%</strong>
              <span className="testbed-sim-winrate-bar">
                <span
                  className="testbed-sim-winrate-fill"
                  style={{ width: `${Math.min(100, winPct)}%` }}
                />
              </span>
            </span>
          )}
        </div>
      </div>
      <div className="testbed-sim-banner__controls">
        {SPEEDS.map(({ label, value }) => (
          <button
            key={value}
            type="button"
            disabled={busySpeed != null}
            className={[
              "testbed-speed-btn",
              activeSpeed === value ? "testbed-speed-btn--active" : "",
            ].join(" ")}
            onClick={() => setSpeed(value)}
          >
            {busySpeed === value ? "…" : `[ ${label} ]`}
          </button>
        ))}
        {error ? <span className="testbed-sim-error">{error}</span> : null}
      </div>
    </div>
  );
}

export default memo(TestbedSimulationBanner);
