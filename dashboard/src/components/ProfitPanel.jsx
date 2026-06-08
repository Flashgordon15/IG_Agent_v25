import { useCallback, useEffect, useState } from "react";
import { fetchV26Profit } from "../api.js";

function Card({ title, children, loading = false }) {
  return (
    <section className="rounded-lg border border-border bg-card p-3 sm:p-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        {title && <h2 className="label-caps">{title}</h2>}
        {loading && (
          <span className="text-[10px] text-muted">Loading…</span>
        )}
      </div>
      {children}
    </section>
  );
}

function fmtMoney(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}£${n.toFixed(2)}`;
}

function fmtPct(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function statusClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "ACTIVE") return "border-success/40 bg-success/10 text-success";
  if (s === "PROBE") return "border-warning/40 bg-warning/10 text-warning";
  if (s === "BANNED") return "border-danger/40 bg-danger/10 text-danger";
  return "border-border bg-bg text-muted";
}

export default function ProfitPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const payload = await fetchV26Profit();
    if (!payload) {
      setError("Could not load v26 profit snapshot.");
      setData(null);
    } else {
      setData(payload);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, 60000);
    return () => window.clearInterval(id);
  }, [load]);

  const ms = data?.milestones;
  const portfolio = data?.portfolio || {};
  const setups = Array.isArray(data?.setups) ? data.setups : [];
  const strategies = data?.shadow_strategies || {};
  const l1 = data?.l1_certification || {};
  const l1m = l1.metrics || {};
  const bars = data?.bar_analysis || {};
  const focus = Array.isArray(data?.learning_focus) ? data.learning_focus : [];
  const shadowSum = data?.shadow_summary?.by_strategy || {};
  const barLabHist = data?.bar_lab_historical || {};
  const barLabStrat = barLabHist.by_strategy || {};
  const walkFwd = data?.walk_forward || {};
  const vetoHints = data?.ohlc_replay?.ml_veto_hints;
  const wfHints =
    (Array.isArray(vetoHints) && vetoHints.length > 0
      ? vetoHints
      : walkFwd.by_epic
        ? Object.entries(walkFwd.by_epic)
            .filter(([, row]) => row.best_wr != null)
            .slice(0, 4)
            .map(
              ([epic, row]) =>
                `${epic}: replay WR ${(row.best_wr * 100).toFixed(0)}% best at ≥${row.recommended_threshold}%`
            )
        : []) || [];
  const envelope = data?.portfolio_envelope || {};
  const liveEnv = envelope.live || {};
  const s1Phase2 = data?.s1_phase2 ?? 0;
  const tradeLearn = data?.trade_learning || {};
  const mlReady = data?.ml_readiness || tradeLearn.ml_readiness || {};
  const replayHist = tradeLearn.replay_historical || {};
  const mlStore = tradeLearn.ml_training_store || {};
  const liveFills = tradeLearn.live_fills || {};

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-muted">
          v26 read-only — rolling expectancy from feeder fills + shadow attribution
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
          <p className="mt-2 text-[11px] text-muted">
            Run:{" "}
            <code className="rounded bg-bg px-1 py-0.5 text-[10px]">
              PYTHONPATH=src:v26 python3 scripts/shadow_compare.py --process --expectancy
            </code>
          </p>
        </Card>
      )}

      <Card title="Milestone" loading={loading && !data}>
        {ms ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-lg font-semibold text-foreground">{ms.current}</span>
              <span className="text-[12px] text-muted">
                target {fmtMoney(ms.target_daily_gbp)}/day
              </span>
              <span className="text-[12px] text-muted">
                rolling avg {fmtMoney(ms.rolling_daily_avg_gbp)}/day
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-bg">
              <div
                className="h-full rounded-full bg-accent transition-all"
                style={{ width: `${Math.min(100, ms.progress_pct || 0)}%` }}
              />
            </div>
            <p className="text-[10px] text-muted">
              {ms.progress_pct?.toFixed?.(1) ?? 0}% of {ms.current} daily target (
              {data?.rolling_days ?? 14}d window)
            </p>
          </div>
        ) : (
          <p className="text-[12px] text-muted">No milestone data yet.</p>
        )}
      </Card>

      <Card title={`Portfolio (${data?.rolling_days ?? 14}d)`} loading={loading && !data}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div>
            <p className="text-[10px] uppercase text-muted">Trades</p>
            <p className="font-mono text-sm">{portfolio.n ?? "—"}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase text-muted">Win rate</p>
            <p className="font-mono text-sm">{fmtPct(portfolio.wr)}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase text-muted">E£ / trade</p>
            <p className="font-mono text-sm">{fmtMoney(portfolio.e_gbp)}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase text-muted">Total P&L</p>
            <p className="font-mono text-sm">{fmtMoney(portfolio.total_pnl_gbp)}</p>
          </div>
        </div>
        {data?.generated_at && (
          <p className="mt-2 text-[10px] text-muted">
            Snapshot: {new Date(data.generated_at).toLocaleString("en-GB")}
          </p>
        )}
      </Card>

      <Card title="L1 certification (learning path)" loading={loading && !data}>
        {l1.status ? (
          <div className="space-y-2 text-[11px]">
            <p>
              <span
                className={[
                  "rounded border px-2 py-0.5 text-[10px] font-semibold uppercase",
                  l1.status === "PASS"
                    ? "border-success/40 bg-success/10 text-success"
                    : "border-warning/40 bg-warning/10 text-warning",
                ].join(" ")}
              >
                {l1.status}
              </span>
              <span className="ml-2 text-muted">
                {l1.days_available ?? 0}/{l1.days_required ?? 90} UTC days
              </span>
            </p>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <div>
                <p className="text-[10px] uppercase text-muted">Total P&L</p>
                <p className="font-mono">{fmtMoney(l1m.total_pnl_gbp)}</p>
              </div>
              <div>
                <p className="text-[10px] uppercase text-muted">Median/day</p>
                <p className="font-mono">{fmtMoney(l1m.median_daily_gbp)}</p>
              </div>
              <div>
                <p className="text-[10px] uppercase text-muted">Days ≥£1k</p>
                <p className="font-mono">{l1m.days_ge_1000_gbp ?? 0}</p>
              </div>
              <div>
                <p className="text-[10px] uppercase text-muted">Would fire</p>
                <p className="font-mono">{l1m.total_would_fire ?? 0}</p>
              </div>
            </div>
            {(bars.total_bars > 0 || Object.keys(shadowSum).length > 0) && (
              <p className="text-muted">
                Bars: {bars.total_bars ?? 0} · S2 trade {bars.s2_would_trade ?? 0} ·
                S3 trade {bars.s3_would_trade ?? 0}
                {Object.entries(shadowSum).map(([sid, row]) => (
                  <span key={sid}>
                    {" "}
                    · {sid} wt={row.would_trade ?? 0}
                  </span>
                ))}
              </p>
            )}
          </div>
        ) : (
          <p className="text-[12px] text-muted">
            Run{" "}
            <code className="rounded bg-bg px-1 text-[10px]">scripts/v26_learning_pack.py</code>
          </p>
        )}
      </Card>

      {(barLabHist.total_bars > 0 ||
        wfHints.length > 0 ||
        envelope.account_balance_gbp != null) && (
        <Card title="Historical learning (OHLC)" loading={loading && !data}>
          <div className="space-y-2 text-[11px]">
            {barLabHist.total_bars > 0 && (
              <p className="text-muted">
                Bar lab: {barLabHist.total_bars ?? 0} bars · S2 wt{" "}
                {barLabStrat.S2_momentum?.would_trade ?? 0} · S3 wt{" "}
                {barLabStrat.S3_session_fx?.would_trade ?? 0}
                {barLabHist.markets != null && (
                  <> · {barLabHist.markets} markets</>
                )}
              </p>
            )}
            {wfHints.length > 0 && (
              <ul className="list-disc space-y-0.5 pl-4 text-muted">
                {wfHints.slice(0, 4).map((hint) => (
                  <li key={hint}>{hint}</li>
                ))}
              </ul>
            )}
            {envelope.account_balance_gbp != null && (
              <p className="font-mono text-[10px] text-muted">
                Envelope £{Number(envelope.account_balance_gbp).toLocaleString()} ·
                avail £{Number(envelope.available_gbp ?? 0).toFixed(0)} ·
                concurrent £{Number(envelope.concurrent_risk_gbp ?? 0).toFixed(0)}/
                {Number(envelope.max_concurrent_risk_gbp ?? 0).toFixed(0)} (
                {envelope.utilization_pct ?? 0}%)
              </p>
            )}
            {liveEnv.gate_enabled && (
              <p className="font-mono text-[10px] text-muted">
                Live risk £{Number(liveEnv.concurrent_risk_gbp ?? 0).toFixed(0)} deployed ·
                daily £{Number(liveEnv.daily_deployed_gbp ?? 0).toFixed(0)} · P&L £
                {Number(liveEnv.daily_pnl_gbp ?? 0).toFixed(0)}
              </p>
            )}
            {s1Phase2 > 0 && (
              <p className="text-[10px] text-muted">
                S1 Phase 2 independent rescores: {s1Phase2} shadow would_trade
              </p>
            )}
          </div>
        </Card>
      )}

      {(replayHist.total_rows > 0 ||
        mlStore.total_records > 0 ||
        liveFills.portfolio?.n > 0) && (
        <Card title="Trade learning (live + historic)" loading={loading && !data}>
          <div className="space-y-2 text-[11px]">
            {replayHist.total_rows > 0 && (
              <p className="text-muted">
                Historic replay: {replayHist.total_rows?.toLocaleString()} labelled signals ·
                fired {replayHist.fired_rows?.toLocaleString()} · decided WR{" "}
                {((replayHist.fired_portfolio?.wr ?? 0) * 100).toFixed(1)}%
              </p>
            )}
            {mlStore.total_records > 0 && (
              <p className="text-muted">
                ML store: {mlStore.total_records} confirmed closes (feature vectors for
                XGBoost)
              </p>
            )}
            {(liveFills.portfolio?.n ?? 0) > 0 && (
              <p className="text-muted">
                Live fills: {liveFills.portfolio.n} · WR{" "}
                {((liveFills.portfolio.wr ?? 0) * 100).toFixed(1)}% · E£{" "}
                {fmtMoney(liveFills.portfolio.e_gbp)}
              </p>
            )}
            {mlReady.min_labelled_rows != null && (
              <p className="font-mono text-[10px] text-muted">
                ml_veto readiness: {mlReady.combined_proxy ?? 0}/
                {mlReady.min_labelled_rows} rows ·{" "}
                {mlReady.ready_for_ml_veto ? "ready (still disabled)" : "not ready"}
              </p>
            )}
            {(tradeLearn.learning_tips || []).slice(0, 2).map((tip) => (
              <p key={tip} className="text-[10px] text-muted">
                {tip}
              </p>
            ))}
          </div>
        </Card>
      )}

      {focus.length > 0 && (
        <Card title="Learning focus" loading={loading && !data}>
          <ul className="list-disc space-y-1 pl-4 text-[11px] text-muted">
            {focus.map((tip) => (
              <li key={tip}>{tip}</li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Today's learning (no-trade days)" loading={loading && !data}>
        {(() => {
          const dp = data?.daily_progress || {};
          const lake = dp.lake || {};
          const gb = data?.gate_blockers || dp.gate_blockers || {};
          const l1 = data?.l1_replay || dp.l1_replay || {};
          const nm = data?.near_miss || dp.shadow_expectancy || {};
          const buckets = gb.confidence_buckets || {};
          if (!lake.signal_evals && !Object.keys(buckets).length) {
            return (
              <p className="text-[12px] text-muted">
                Run{" "}
                <code className="rounded bg-bg px-1 text-[10px]">
                  scripts/v26_progress.py --write
                </code>{" "}
                to populate gate + counterfactual analysis.
              </p>
            );
          }
          return (
            <div className="space-y-3 text-[11px]">
              <p className="text-muted">
                Feeder today: {lake.signal_evals ?? "—"} evals · would_fire{" "}
                {lake.would_fire ?? 0} · fills {lake.fill_closes ?? 0}
              </p>
              {Object.keys(buckets).length > 0 && (
                <div>
                  <p className="mb-1 text-[10px] uppercase text-muted">Confidence %</p>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(buckets).map(([k, v]) => (
                      <span
                        key={k}
                        className="rounded border border-border bg-bg px-2 py-0.5 font-mono"
                      >
                        {k}: {v}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {l1.median_confidence != null && (
                <p>
                  L1 replay: median <strong>{l1.median_confidence}%</strong>
                  {l1.by_threshold?.[">=75"] != null && (
                    <> · ≥75% would fire <strong>{l1.by_threshold[">=75"]}</strong>×</>
                  )}
                </p>
              )}
              {nm.near_miss_evals > 0 && (
                <p className="text-muted">
                  Near-miss 70–79%: {nm.near_miss_evals} evals · shadow match{" "}
                  {nm.shadow_would_trade_same_epic ?? 0} · est setup E£ proxy{" "}
                  {fmtMoney(nm.estimated_counterfactual_e_gbp)}
                </p>
              )}
            </div>
          );
        })()}
      </Card>

      <Card title="Shadow strategy P&L" loading={loading && !data}>
        {Object.keys(strategies).length === 0 ? (
          <p className="text-[12px] text-muted">No attributed shadow fills yet.</p>
        ) : (
          <>
            <p className="mb-2 text-[10px] text-muted">
              {data.shadow_attributed_fills}/{data.shadow_total_fills} live fills matched
            </p>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[320px] text-left text-[11px]">
                <thead>
                  <tr className="border-b border-border text-muted">
                    <th className="py-1 pr-2">Strategy</th>
                    <th className="py-1 pr-2">N</th>
                    <th className="py-1 pr-2">WR</th>
                    <th className="py-1 pr-2">E£</th>
                    <th className="py-1">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(strategies).map(([sid, row]) => (
                    <tr key={sid} className="border-b border-border/50">
                      <td className="py-1.5 pr-2 font-medium">{sid}</td>
                      <td className="py-1.5 pr-2 font-mono">{row.n}</td>
                      <td className="py-1.5 pr-2 font-mono">{fmtPct(row.wr)}</td>
                      <td className="py-1.5 pr-2 font-mono">{fmtMoney(row.e_gbp)}</td>
                      <td className="py-1.5 font-mono">{fmtMoney(row.total_pnl_gbp)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      <Card title="Setup league table" loading={loading && !data}>
        {setups.length === 0 ? (
          <p className="text-[12px] text-muted">No setup stats yet.</p>
        ) : (
          <div className="max-h-[50vh] overflow-auto">
            <table className="w-full min-w-[480px] text-left text-[11px]">
              <thead className="sticky top-0 bg-card">
                <tr className="border-b border-border text-muted">
                  <th className="py-1 pr-2">Setup</th>
                  <th className="py-1 pr-2">N</th>
                  <th className="py-1 pr-2">WR</th>
                  <th className="py-1 pr-2">E£</th>
                  <th className="py-1">Status</th>
                </tr>
              </thead>
              <tbody>
                {setups.slice(0, 25).map((row) => (
                  <tr key={row.setup_key} className="border-b border-border/50">
                    <td className="max-w-[200px] truncate py-1.5 pr-2" title={row.setup_key}>
                      {row.setup_key}
                    </td>
                    <td className="py-1.5 pr-2 font-mono">{row.n}</td>
                    <td className="py-1.5 pr-2 font-mono">{fmtPct(row.wr)}</td>
                    <td className="py-1.5 pr-2 font-mono">{fmtMoney(row.e_gbp)}</td>
                    <td className="py-1.5">
                      <span
                        className={[
                          "rounded border px-1.5 py-0.5 text-[9px] font-semibold uppercase",
                          statusClass(row.status),
                        ].join(" ")}
                      >
                        {row.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
