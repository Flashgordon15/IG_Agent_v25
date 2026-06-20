import { memo, useEffect, useMemo, useState } from "react";
import { api } from "../api/client.js";
import { normalizeHudError } from "../utils/hudErrors.js";
import useThrottledValue from "../hooks/useThrottledValue.js";

function Card({ title, children, titleRight = null }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-[12px] font-semibold uppercase tracking-wide text-muted">{title}</h3>
        {titleRight}
      </div>
      {children}
    </div>
  );
}

function pct(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return `${Math.round(Number(v) * 100)}%`;
}

function money(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n >= 0 ? "+" : "";
  return `${sign}£${n.toFixed(2)}`;
}

function splinePath(points, key, width, height) {
  if (!points.length) return "";
  const xs = points.map((_, i) => (i / Math.max(1, points.length - 1)) * width);
  const vals = points.map((p) => Number(p[key] || 0));
  const min = Math.min(...vals, 0);
  const max = Math.max(...vals, 0.001);
  const ys = vals.map((v) => height - ((v - min) / (max - min)) * (height - 8) - 4);
  if (points.length === 1) return `M ${xs[0]} ${ys[0]}`;
  let d = `M ${xs[0]} ${ys[0]}`;
  for (let i = 1; i < points.length; i += 1) {
    const cx = (xs[i - 1] + xs[i]) / 2;
    d += ` Q ${cx} ${ys[i - 1]}, ${xs[i]} ${ys[i]}`;
  }
  return d;
}

function StatsTab() {
  const [data, setData] = useState(null);
  const [triage, setTriage] = useState(null);
  const [error, setError] = useState("");
  const [sortKey, setSortKey] = useState("net_pnl_gbp");
  const [testbedStatus, setTestbedStatus] = useState(null);
  const throttledData = useThrottledValue(data, 100);
  const throttledTriage = useThrottledValue(triage, 100);

  useEffect(() => {
    let cancelled = false;
    const pollTestbed = async () => {
      try {
        const s = await api.testbedStatus();
        if (!cancelled) setTestbedStatus(s?.hardened_testbed ? s : null);
      } catch {
        if (!cancelled) setTestbedStatus(null);
      }
    };
    pollTestbed();
    const id = setInterval(pollTestbed, 250);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [payload, triagePayload] = await Promise.all([
          api.edgeAnalysis(),
          api.triageStats(),
        ]);
        if (!cancelled) {
          setData(payload);
          setTriage(triagePayload);
          setError("");
        }
      } catch (e) {
        if (!cancelled) setError(normalizeHudError(e.message || "Failed to load edge analysis"));
      }
    };
    load();
    const id = setInterval(load, 5 * 60 * 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const instruments = useMemo(() => {
    const rows = [...(throttledData?.by_instrument || [])];
    rows.sort((a, b) => Number(b[sortKey] ?? 0) - Number(a[sortKey] ?? 0));
    return rows;
  }, [throttledData, sortKey]);

  const wave = throttledTriage?.wave ?? [];
  const slipPath = useMemo(() => splinePath(wave, "slip", 320, 72), [wave]);
  const spreadPath = useMemo(() => splinePath(wave, "spread", 320, 72), [wave]);

  if (error) {
    return <div className="p-4 text-sm apex-hud-backfill">{error}</div>;
  }
  if (!throttledData) {
    return <div className="p-4 text-sm text-muted">Loading edge analysis…</div>;
  }

  const o = throttledData.overall || {};
  const ml = throttledData.ml_readiness || {};
  const mlPct = Math.min(
    100,
    throttledTriage?.ml_readiness_pct ?? ml.percentage_ready ?? 0,
  );

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-2 sm:p-4">
      {testbedStatus && (
        <div className="testbed-live-winrate-card">
          <span>TESTBED Simulation Win Rate</span>
          <strong>{testbedStatus.win_rate_pct ?? "—"}%</strong>
          <div className="testbed-sim-winrate-bar testbed-sim-winrate-bar--lg">
            <span
              className="testbed-sim-winrate-fill"
              style={{
                width: `${Math.min(100, Number(testbedStatus.win_rate_pct) || 0)}%`,
              }}
            />
          </div>
          <p className="testbed-stats-caption">
            Virtual {testbedStatus.virtual_time_hms ?? "—"} ·{" "}
            {testbedStatus.ticks_per_sec ?? 0} ticks/sec ·{" "}
            {testbedStatus.total_ticks?.toLocaleString() ?? 0} ingested
          </p>
        </div>
      )}
      <Card title="Triage session metrics (triage_v30.db)">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[
            ["Sharpe ratio", throttledTriage?.sharpe_ratio?.toFixed(2) ?? "—"],
            ["Avg slippage (pts)", throttledTriage?.avg_slip_pts ?? "—"],
            ["Spread premium (pts)", throttledTriage?.avg_spread_premium_pts ?? "—"],
            ["Expectancy", money(throttledTriage?.expectancy_gbp)],
          ].map(([label, value]) => (
            <div key={label} className="rounded-lg border border-border bg-surface/40 p-3">
              <p className="label-caps">{label}</p>
              <p className="mt-1 text-lg font-semibold tabular-nums hud-value-positive">{value}</p>
            </div>
          ))}
        </div>
        {wave.length > 0 && (
          <svg className="apex-stats-wave mt-3" viewBox="0 0 320 72" preserveAspectRatio="none">
            <path className="slip" d={slipPath} />
            <path className="spread" d={spreadPath} />
          </svg>
        )}
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["Total trades", o.total_trades ?? 0],
          ["Win rate", pct(o.win_rate)],
          ["Profit factor", o.profit_factor ?? "—"],
          ["Expectancy / trade", money(o.expectancy_per_trade_gbp)],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg border border-border bg-card p-3">
            <p className="label-caps">{label}</p>
            <p className="mt-1 text-lg font-semibold tabular-nums text-foreground">{value}</p>
          </div>
        ))}
      </div>

      <Card title="ML readiness">
        <div className="space-y-2">
          <div className="flex justify-between text-[12px] text-foreground">
            <span>
              {ml.confirmed_live_trades ?? 0} / {ml.trades_needed_for_ml ?? 50} confirmed live trades
            </span>
            <span className="hud-value-warning">{mlPct}%</span>
          </div>
          <div className="apex-neon-ml-progress">
            <div
              className="apex-neon-ml-progress__fill"
              style={{ width: `${mlPct}%` }}
            />
          </div>
          {ml.estimated_ready_date && (
            <p className="text-[11px] text-muted">Estimated ML-ready: {ml.estimated_ready_date}</p>
          )}
        </div>
      </Card>

      <Card
        title="By instrument"
        titleRight={(
          <select
            className="rounded border border-border bg-background px-2 py-1 text-[11px]"
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value)}
          >
            <option value="net_pnl_gbp">Net P&L</option>
            <option value="win_rate">Win rate</option>
            <option value="profit_factor">Profit factor</option>
          </select>
        )}
      >
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-[11px]">
            <thead>
              <tr>
                <th className="px-2 py-1">Instrument</th>
                <th className="px-2 py-1">Trades</th>
                <th className="px-2 py-1">Win%</th>
                <th className="px-2 py-1">PF</th>
                <th className="px-2 py-1">Net P&L</th>
                <th className="px-2 py-1">Avg mins</th>
              </tr>
            </thead>
            <tbody>
              {instruments.map((row) => (
                <tr key={row.epic} className="border-t border-border/60">
                  <td className="px-2 py-1.5 text-foreground">{row.display_name || row.epic}</td>
                  <td className="px-2 py-1.5 tabular-nums">{row.trades}</td>
                  <td className="px-2 py-1.5 tabular-nums">{pct(row.win_rate)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{row.profit_factor ?? "—"}</td>
                  <td className="px-2 py-1.5 tabular-nums hud-value-positive">{money(row.net_pnl_gbp)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{row.avg_duration_minutes ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="By session">
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-[11px]">
            <thead>
              <tr>
                <th className="px-2 py-1">Session</th>
                <th className="px-2 py-1">Trades</th>
                <th className="px-2 py-1">Win%</th>
                <th className="px-2 py-1">PF</th>
                <th className="px-2 py-1">Net P&L</th>
              </tr>
            </thead>
            <tbody>
              {(data.by_session || []).map((row) => (
                <tr key={row.session} className="border-t border-border/60">
                  <td className="px-2 py-1.5">{row.session}</td>
                  <td className="px-2 py-1.5 tabular-nums">{row.trades}</td>
                  <td className="px-2 py-1.5 tabular-nums">{pct(row.win_rate)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{row.profit_factor ?? "—"}</td>
                  <td className="px-2 py-1.5 tabular-nums">{money(row.net_pnl_gbp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="By hour (BST) — heatmap">
        <div className="grid grid-cols-4 gap-1 sm:grid-cols-6 md:grid-cols-8">
          {(data.by_hour_bst || []).map((row) => {
            const pnl = Number(row.net_pnl_gbp || 0);
            const tone = pnl > 0 ? "bg-green/20 text-green" : pnl < 0 ? "bg-red/20 text-red" : "bg-border/40 text-muted";
            return (
              <div key={row.hour} className={`rounded px-2 py-2 text-center text-[10px] ${tone}`}>
                <div className="font-semibold">{String(row.hour).padStart(2, "0")}:00</div>
                <div className="tabular-nums">{money(row.net_pnl_gbp)}</div>
                <div className="tabular-nums opacity-80">{row.trades}t</div>
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

export default memo(StatsTab);
