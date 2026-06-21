import { useEffect, useState } from "react";
import { fetchAlphaMatrixOps } from "../api.js";

function ringStateClass(state) {
  const s = String(state || "").toLowerCase();
  if (s === "mapped") return "alpha-terminal-metric alpha-terminal-metric--ok";
  return "alpha-terminal-metric alpha-terminal-metric--fault";
}

export default function AlphaMatrixOpsConsole() {
  const [payload, setPayload] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const data = await fetchAlphaMatrixOps();
      if (!cancelled) setPayload(data || null);
    };
    load();
    const id = window.setInterval(load, 500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const ring = payload?.hardware_ring_buffer || {};
  const latency = payload?.processing_latency_us || {};

  return (
    <div className="alpha-terminal">
      <header className="alpha-terminal-header">
        <span className="alpha-terminal-tag">:9199</span>
        <h1>Alpha Matrix Status</h1>
        <span className="alpha-terminal-clock">
          {payload?.updated_at ? payload.updated_at.replace("T", " ").slice(0, 19) : "—"} UTC
        </span>
      </header>

      <section className="alpha-terminal-grid">
        <article className="alpha-terminal-panel">
          <h2>Active Cache Volume</h2>
          <p className="alpha-terminal-value tabular-nums">
            {payload?.active_cache_volume ?? "—"}
          </p>
          <p className="alpha-terminal-hint">Cached pattern vectors in SHM table</p>
        </article>

        <article className="alpha-terminal-panel">
          <h2>Hardware Ring Buffer</h2>
          <p className={ringStateClass(ring.state)}>
            {String(ring.state || "unknown").toUpperCase()}
          </p>
          <p className="alpha-terminal-hint font-mono text-xs">
            {ring.posix_path || "/ig_agent_v30_alpha_matrix"}
          </p>
          <p className="alpha-terminal-sub">
            pulse={ring.pulse_seq ?? 0} miss={ring.miss_cycles ?? 0} unmap=
            {ring.unmap_count ?? 0}
          </p>
        </article>

        <article className="alpha-terminal-panel">
          <h2>Processing Latency</h2>
          <p className="alpha-terminal-value tabular-nums">
            {(latency.last_us ?? 0).toFixed(1)} µs
          </p>
          <p className="alpha-terminal-sub">
            p50 {(latency.p50_us ?? 0).toFixed(1)} µs · p99 {(latency.p99_us ?? 0).toFixed(1)} µs
          </p>
          <p className="alpha-terminal-hint">Naked pointer lookup profile</p>
        </article>
      </section>
    </div>
  );
}
