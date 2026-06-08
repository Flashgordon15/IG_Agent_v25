import { useCallback, useEffect, useState } from "react";

function Card({ title, children, loading = false }) {
  return (
    <section className="rounded-lg border border-border bg-card p-3 sm:p-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        {title && <h2 className="label-caps">{title}</h2>}
        {loading && <span className="text-[10px] text-muted">Loading…</span>}
      </div>
      {children}
    </section>
  );
}

function statusColor(status) {
  const s = String(status || "").toUpperCase();
  if (s === "PASS") return "text-success";
  if (s === "NOT_STARTED") return "text-muted";
  if (s === "INSUFFICIENT" || s === "IN_PROGRESS") return "text-warning";
  return "text-muted";
}

export default function CertPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await fetch("/api/v26/cert");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(e?.message || "Failed to load certification");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, 60_000);
    return () => window.clearInterval(id);
  }, [load]);

  const levels = Array.isArray(data?.levels) ? data.levels : [];
  const focus = Array.isArray(data?.learning_focus) ? data.learning_focus : [];
  const forwardDaily = Array.isArray(data?.forward_daily) ? data.forward_daily : [];
  const gateRelax = data?.gate_relaxation?.active || {};
  const relaxRecs = Array.isArray(data?.gate_relaxation?.recommendations)
    ? data.gate_relaxation.recommendations
    : [];

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-muted">
          v26 certification ladder — target {data?.target || "L5"} · milestone{" "}
          {data?.current_milestone || "M0"}
        </p>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="rounded border border-border px-3 py-1 text-[11px] text-muted hover:text-foreground disabled:opacity-50"
        >
          Refresh
        </button>
      </div>

      {error && (
        <Card title="Unavailable">
          <p className="text-[12px] text-danger">{error}</p>
        </Card>
      )}

      <Card title="Certification levels" loading={loading && !data}>
        {levels.length > 0 ? (
          <div className="space-y-3">
            {levels.map((lv) => (
              <div key={lv.id} className="space-y-1">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-[12px] font-semibold text-foreground">
                    {lv.id} — {lv.name}
                  </span>
                  <span className={`text-[11px] font-mono ${statusColor(lv.status)}`}>
                    {lv.status} · {lv.pct ?? 0}%
                  </span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-bg">
                  <div
                    className="h-full rounded-full bg-accent transition-all"
                    style={{ width: `${Math.min(100, lv.pct || 0)}%` }}
                  />
                </div>
                <p className="text-[10px] text-muted">{lv.detail}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-[12px] text-muted">Run v26_learning_pack.py to populate cert data.</p>
        )}
      </Card>

      {forwardDaily.length > 0 && (
        <Card title="L4 demo forward — daily P&L">
          <div className="flex items-end gap-0.5 h-16">
            {forwardDaily.map((d) => {
              const pnl = Number(d.fill_pnl_gbp) || 0;
              const h = Math.min(100, Math.max(4, Math.abs(pnl) * 2));
              const pos = pnl >= 0;
              return (
                <div
                  key={d.day}
                  className="flex flex-1 flex-col items-center justify-end gap-0.5"
                  title={`${d.day}: £${pnl.toFixed(0)} (${d.fill_closes || 0} fills)`}
                >
                  <div
                    className={`w-full max-w-[14px] rounded-t ${pos ? "bg-success/70" : "bg-danger/70"}`}
                    style={{ height: `${h}%` }}
                  />
                  <span className="text-[8px] text-muted tabular-nums">
                    {String(d.day || "").slice(8)}
                  </span>
                </div>
              );
            })}
          </div>
          <p className="mt-2 text-[10px] text-muted">
            Feeder fill_close P&L per UTC day (v25 demo execution)
          </p>
        </Card>
      )}

      {gateRelax.enabled && (
        <Card title="Active gate relaxation">
          <p className="text-[11px] text-muted">
            Fitness floor {gateRelax.fitness_min}% on{" "}
            {(gateRelax.epics || []).join(", ") || "—"}
            {gateRelax.require_points_healthy ? " (points HEALTHY only)" : ""}
          </p>
          {gateRelax.note && (
            <p className="mt-1 text-[10px] text-muted/80">{gateRelax.note}</p>
          )}
        </Card>
      )}

      {relaxRecs.length > 0 && (
        <Card title="Relaxation recommendations">
          <ul className="list-disc space-y-1 pl-4 text-[11px] text-muted">
            {relaxRecs.slice(0, 4).map((r) => (
              <li key={r.id || r.action}>
                {r.safe ? "✓ " : "? "}
                {r.action}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {focus.length > 0 && (
        <Card title="Learning focus">
          <ul className="list-disc space-y-1 pl-4 text-[11px] text-muted">
            {focus.slice(0, 6).map((tip) => (
              <li key={tip}>{tip}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
