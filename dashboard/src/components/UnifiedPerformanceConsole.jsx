import { useEffect, useState } from "react";
import { fetchUnifiedPerformance } from "../api.js";

function syncLabel(synced) {
  return synced ? "TRUE SYNC" : "FALSE SYNC";
}

function syncClass(synced) {
  return synced
    ? "alpha-terminal-metric alpha-terminal-metric--ok"
    : "alpha-terminal-metric alpha-terminal-metric--fault";
}

export default function UnifiedPerformanceConsole() {
  const [payload, setPayload] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const data = await fetchUnifiedPerformance();
      if (!cancelled) setPayload(data || null);
    };
    load();
    const id = window.setInterval(load, 250);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const align = payload?.thread_alignment || {};
  const latency = payload?.e2e_latency_ns || {};
  const threads = payload?.threads || {};
  const feedBanner =
    payload?.stream_mapping_banner ||
    payload?.multi_feed_hub?.stream_mapping_banner ||
    "🔴 Feed hub initializing";

  return (
    <div className="alpha-terminal">
      <header className="alpha-terminal-header">
        <span className="alpha-terminal-tag">UNIFIED :8080</span>
        <h1>Performance Telemetry</h1>
        <span className="alpha-terminal-clock">
          pid {threads.master_pid ?? "—"} · A=
          {threads.a_alive ? "UP" : "—"} B={threads.b_alive ? "UP" : "—"}
        </span>
      </header>

      <p className="alpha-terminal-feed-banner">{feedBanner}</p>

      <section className="alpha-terminal-grid">
        <article className="alpha-terminal-panel">
          <h2>Thread A &amp; B Memory Alignment</h2>
          <p className={syncClass(align.synced)}>
            {syncLabel(align.synced)}
          </p>
          <p className="alpha-terminal-sub">
            write={align.thread_a_write_seq ?? 0} read={align.thread_b_read_seq ?? 0}{" "}
            gen={align.compile_generation ?? 0}
          </p>
        </article>

        <article className="alpha-terminal-panel">
          <h2>End-to-End Latency</h2>
          <p className="alpha-terminal-value tabular-nums">
            {(latency.last_ns ?? 0).toLocaleString()} ns
          </p>
          <p className="alpha-terminal-sub">
            p50 {(latency.p50_ns ?? 0).toLocaleString()} ns · p99{" "}
            {(latency.p99_ns ?? 0).toLocaleString()} ns
          </p>
        </article>

        <article className="alpha-terminal-panel">
          <h2>Pre-Baked Vector Density</h2>
          <p className="alpha-terminal-value tabular-nums">
            {payload?.vector_density ?? 0}
          </p>
          <p className="alpha-terminal-hint">Compiled pattern states in RAM</p>
        </article>
      </section>
    </div>
  );
}
