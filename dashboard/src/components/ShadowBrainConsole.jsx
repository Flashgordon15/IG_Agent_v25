import { useEffect, useState } from "react";
import { fetchShadowBrain } from "../api.js";

function statusClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "HEALTHY") return "shadow-brain-status shadow-brain-status--ok";
  if (s === "DEGRADED") return "shadow-brain-status shadow-brain-status--warn";
  return "shadow-brain-status shadow-brain-status--bad";
}

function formatFloor(value, suffix = "") {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  if (suffix === "%") return `${n.toFixed(2)}%`;
  if (suffix === "prob") return n.toFixed(4);
  return String(n);
}

export default function ShadowBrainConsole() {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await fetchShadowBrain();
        if (!cancelled) {
          setPayload(data || null);
          setError(data ? "" : "Brain telemetry unavailable");
        }
      } catch (err) {
        if (!cancelled) {
          setError(err?.message || "Brain telemetry unavailable");
        }
      }
    };
    load();
    const id = window.setInterval(load, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const health = payload?.data_health || {};
  const funnel = payload?.gate_funnel?.sequential_dropouts || [];
  const tolerance = payload?.live_tolerance_output || {};
  const baseline = tolerance.baseline_floors || {};
  const active = tolerance.active_floors || {};
  const lastPublish = tolerance.last_publish || {};

  return (
    <div className="shadow-brain-console space-y-4">
      <header className="shadow-brain-hero">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
            Shadow Brain · Port 9199
          </p>
          <h1 className="text-2xl font-semibold text-foreground">
            Operational Intelligence Console
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Yahoo data health · sequential gate funnel · live tolerance handoff
          </p>
        </div>
        <div className={statusClass(health.status)}>
          {health.status || "UNKNOWN"}
        </div>
      </header>

      {error ? (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
          {error}
        </div>
      ) : null}

      <section className="shadow-brain-grid">
        <article className="shadow-brain-card">
          <h2>1 · Incoming Tick Health</h2>
          <dl className="shadow-brain-metrics">
            <div>
              <dt>Total ticks</dt>
              <dd>{health.total_incoming_ticks ?? "—"}</dd>
            </div>
            <div>
              <dt>Yahoo / replay path</dt>
              <dd>{health.yahoo_ticks ?? "—"}</dd>
            </div>
            <div>
              <dt>Stale ticks</dt>
              <dd>{health.stale_ticks ?? "—"}</dd>
            </div>
            <div>
              <dt>All gates passed</dt>
              <dd>{health.all_passed_ticks ?? "—"}</dd>
            </div>
          </dl>
        </article>

        <article className="shadow-brain-card shadow-brain-card--wide">
          <h2>2 · Sequential Gate Dropout Funnel</h2>
          {funnel.length === 0 ? (
            <p className="text-sm text-muted-foreground">No funnel data yet.</p>
          ) : (
            <div className="shadow-brain-funnel">
              {funnel.map((row) => (
                <div key={row.gate} className="shadow-brain-funnel-row">
                  <div className="shadow-brain-funnel-label">
                    <span>{row.gate}</span>
                    <span className="text-muted-foreground">{row.top_reason}</span>
                  </div>
                  <div className="shadow-brain-funnel-bar-wrap">
                    <div
                      className="shadow-brain-funnel-bar"
                      style={{
                        width: `${Math.min(100, (row.dropouts / Math.max(1, funnel[0]?.dropouts || 1)) * 100)}%`,
                      }}
                    />
                  </div>
                  <div className="shadow-brain-funnel-count">{row.dropouts}</div>
                </div>
              ))}
            </div>
          )}
        </article>

        <article className="shadow-brain-card shadow-brain-card--wide">
          <h2>3 · Active Threshold Tuning (Live Handoff)</h2>
          <div className="shadow-brain-tolerance-grid">
            <div>
              <p className="shadow-brain-tol-label">Signal confidence floor</p>
              <p className="shadow-brain-tol-value">
                {formatFloor(active.signal_threshold_floor, "%")}
              </p>
              <p className="shadow-brain-tol-base">
                baseline {formatFloor(baseline.signal_threshold_floor, "%")}
              </p>
            </div>
            <div>
              <p className="shadow-brain-tol-label">Environment fitness floor</p>
              <p className="shadow-brain-tol-value">
                {formatFloor(active.fitness_min_floor, "%")}
              </p>
              <p className="shadow-brain-tol-base">
                baseline {formatFloor(baseline.fitness_min_floor, "%")}
              </p>
            </div>
            <div>
              <p className="shadow-brain-tol-label">ML veto probability</p>
              <p className="shadow-brain-tol-value">
                {formatFloor(active.ml_veto_min_probability, "prob")}
              </p>
              <p className="shadow-brain-tol-base">
                baseline {formatFloor(baseline.ml_veto_min_probability, "prob")}
              </p>
            </div>
          </div>
          {lastPublish?.near_miss_gate ? (
            <p className="mt-3 text-xs text-muted-foreground">
              Last near-miss tune: {lastPublish.near_miss_gate} @ {lastPublish.margin_pct}%
              · {lastPublish.published_at_utc || "—"}
            </p>
          ) : (
            <p className="mt-3 text-xs text-muted-foreground">
              No near-miss tolerance publish yet this session.
            </p>
          )}
        </article>
      </section>
    </div>
  );
}
