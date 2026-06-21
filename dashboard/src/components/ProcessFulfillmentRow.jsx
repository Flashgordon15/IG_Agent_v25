import { useEffect, useState } from "react";
import { fetchUnifiedFulfillment } from "../api.js";

const REFRESH_MS = 1000;

function stageClass(ok) {
  return ok
    ? "fulfillment-stage fulfillment-stage--ok"
    : "fulfillment-stage fulfillment-stage--warn";
}

export default function ProcessFulfillmentRow() {
  const [payload, setPayload] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const data = await fetchUnifiedFulfillment();
      if (!cancelled) setPayload(data || null);
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const stages = Array.isArray(payload?.stages) ? payload.stages : [];

  return (
    <div className="fulfillment-shell">
      <header className="fulfillment-header">
        <span className="fulfillment-tag">UNIFIED :8080</span>
        <h1>4-Stage Process Fulfillment</h1>
        <span className="fulfillment-clock">
          refresh {payload?.refresh_ms ?? REFRESH_MS}ms ·{" "}
          {payload?.updated_at ?? "—"}
        </span>
      </header>

      <p className="fulfillment-banner">
        {payload?.stream_mapping_banner ??
          "🟡 Feed hub initializing"}
      </p>

      <div className="fulfillment-row">
        {stages.length === 0 ? (
          <article className="fulfillment-stage fulfillment-stage--warn">
            <span className="fulfillment-stage-num">—</span>
            <div>
              <h2>Bootstrapping</h2>
              <p>Awaiting decoupled fulfillment cache…</p>
            </div>
          </article>
        ) : (
          stages.map((stage) => (
            <article
              key={stage.id ?? stage.name}
              className={stageClass(Boolean(stage.ok))}
            >
              <span className="fulfillment-stage-num">
                Stage {stage.id}
              </span>
              <div>
                <h2>{stage.name}</h2>
                <p>{stage.label}</p>
              </div>
            </article>
          ))
        )}
      </div>

      {payload?.last_performance_row ? (
        <p className="fulfillment-perf">
          Last closed: {payload.last_performance_row.result} ·{" "}
          {payload.last_performance_row.epic} · conf{" "}
          {payload.last_performance_row.confidence}% ·{" "}
          {payload.last_performance_row.latency_us}µs lookup
        </p>
      ) : null}
    </div>
  );
}
