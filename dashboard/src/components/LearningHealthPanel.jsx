import { useEffect, useState } from "react";

function Pill({ ok, label, detail }) {
  return (
    <div className="flex flex-col gap-0.5 rounded border border-border bg-surface/60 px-2 py-1.5">
      <span className="text-[10px] uppercase tracking-wide text-muted">{label}</span>
      <span className={`text-[12px] font-semibold tabular-nums ${ok ? "text-success" : "text-warning"}`}>
        {detail}
      </span>
    </div>
  );
}

export default function LearningHealthPanel() {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/learning-health")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((data) => {
        if (!cancelled) setReport(data);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e.message || e));
      });
    const id = setInterval(() => {
      fetch("/api/learning-health")
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => data && setReport(data))
        .catch(() => {});
    }, 60000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (error) {
    return (
      <section className="rounded-lg border border-border bg-card p-3 sm:p-4">
        <h2 className="label-caps mb-2">Learning health</h2>
        <p className="text-[12px] text-danger">Unavailable ({error})</p>
      </section>
    );
  }

  if (!report) {
    return (
      <section className="rounded-lg border border-border bg-card p-3 sm:p-4">
        <h2 className="label-caps mb-2">Learning health</h2>
        <p className="text-[12px] text-muted">Loading…</p>
      </section>
    );
  }

  const pnl = report.agent_pnl || {};
  const ml = report.ml || {};
  const reg = report.setup_registry || {};
  const prot = report.policy?.protective_learning || {};
  const recs = report.recommendations || [];

  return (
    <section className="rounded-lg border border-border bg-card p-3 sm:p-4">
      <h2 className="label-caps mb-2">Learning health</h2>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Pill
          ok={(pnl.agent_closed_trades || 0) >= 10}
          label="Agent closes"
          detail={`${pnl.agent_closed_trades ?? 0} (${Math.round((pnl.agent_win_rate || 0) * 100)}% WR)`}
        />
        <Pill
          ok={Boolean(ml.ml_blend_ready)}
          label="ML blend"
          detail={
            ml.ml_blend_ready
              ? "Ready"
              : `${ml.training_records ?? 0}/${ml.training_records_required ?? 500}`
          }
        />
        <Pill
          ok={Boolean(reg.enabled && (reg.banned_count || 0) > 0)}
          label="Setup bans"
          detail={reg.enabled ? `${reg.banned_count ?? 0} banned` : "Gate off"}
        />
        <Pill
          ok={Boolean(prot.enabled)}
          label="Protective"
          detail={prot.enabled ? `≥${prot.signal_threshold_floor ?? "—"}% conf` : "Off"}
        />
      </div>
      {recs.length > 0 && (
        <ul className="mt-3 space-y-1 text-[11px] text-muted">
          {recs.slice(0, 4).map((line) => (
            <li key={line}>• {line}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
