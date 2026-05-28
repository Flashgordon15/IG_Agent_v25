import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

function Card({ title, children }) {
  return (
    <section className="card p-4 mb-4">
      <h2 className="text-sm font-semibold text-muted uppercase tracking-wide mb-3">{title}</h2>
      {children}
    </section>
  );
}

export default function IntelligenceTab() {
  const [replay, setReplay] = useState(null);
  const [shadow, setShadow] = useState(null);
  const [learning, setLearning] = useState(null);
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const [r, s, l] = await Promise.all([
        api.replaySummary(),
        api.shadowToday(),
        api.learningStatus(),
      ]);
      setReplay(r);
      setShadow(s);
      setLearning(l);
      setErr(null);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const runAnalysis = async () => {
    setRunning(true);
    setRunResult(null);
    try {
      const res = await api.replayRun();
      setRunResult(res);
      await load();
    } catch (e) {
      setRunResult({ ok: false, error: String(e.message || e) });
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="p-4 max-w-3xl">
      {err && <p className="text-red text-sm mb-3">{err}</p>}

      <Card title="Replay summary">
        {replay ? (
          <ul className="text-sm space-y-1">
            <li>Bars analysed: {replay.bars_analysed ?? 0} (cache {replay.bars_cache ?? 0})</li>
            <li>
              Date range: {replay.date_from || "—"} → {replay.date_to || "—"}
            </li>
            <li>{replay.best_threshold || "Best threshold: —"}</li>
            <li>{replay.best_rsi || "Best RSI: —"}</li>
            <li>Last updated: {replay.last_updated || "—"}</li>
            {(replay.recommendations || []).map((rec) => (
              <li key={rec} className="text-amber">
                {rec}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted text-sm">Loading…</p>
        )}
        <button
          type="button"
          className="mt-3 px-3 py-1.5 rounded border border-border text-sm hover:bg-surface"
          disabled={running}
          onClick={runAnalysis}
        >
          {running ? "Running…" : "Run analysis"}
        </button>
        {runResult && (
          <pre className="mt-2 text-xs text-muted whitespace-pre-wrap">
            {runResult.ok ? "Replay pipeline finished." : runResult.error || runResult.stderr}
          </pre>
        )}
      </Card>

      <Card title="Signal quality (shadow, today)">
        {shadow ? (
          <ul className="text-sm space-y-1">
            <li>Evaluations: {shadow.evaluations}</li>
            <li>Would-have-traded: {shadow.would_have_traded}</li>
            <li>Top blocked setup: {shadow.top_blocked_setup || "—"}</li>
            <li>
              Est. extra signals if threshold −5pts: {shadow.estimated_extra_if_threshold_minus_5}
            </li>
          </ul>
        ) : (
          <p className="text-muted text-sm">Loading…</p>
        )}
      </Card>

      <Card title="Learning status">
        {learning ? (
          <>
            <ul className="text-sm space-y-1 mb-3">
              <li>ML training records: {learning.ml_records}</li>
              <li>Confirmed trades (DB): {learning.confirmed_trades}</li>
            </ul>
            <div className="h-2 bg-border rounded overflow-hidden mb-2">
              <div
                className="h-full bg-green"
                style={{ width: `${learning.ml_viability_pct}%` }}
              />
            </div>
            <p className="text-xs text-muted mb-2">
              {learning.ml_records} / {learning.ml_viability_target} records for ML viability
            </p>
            <ul className="text-sm">
              {(learning.top_setups || []).map((s) => (
                <li key={s.setup_key}>
                  {s.setup_key}: {s.count}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="text-muted text-sm">Loading…</p>
        )}
      </Card>
    </div>
  );
}
