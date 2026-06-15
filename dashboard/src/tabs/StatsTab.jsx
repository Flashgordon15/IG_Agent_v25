import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client.js";

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

export default function StatsTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [sortKey, setSortKey] = useState("net_pnl_gbp");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await api.edgeAnalysis();
        if (!cancelled) {
          setData(payload);
          setError("");
        }
      } catch (e) {
        if (!cancelled) setError(e.message || "Failed to load edge analysis");
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
    const rows = [...(data?.by_instrument || [])];
    rows.sort((a, b) => Number(b[sortKey] ?? 0) - Number(a[sortKey] ?? 0));
    return rows;
  }, [data, sortKey]);

  if (error) {
    return <div className="p-4 text-sm text-danger">{error}</div>;
  }
  if (!data) {
    return <div className="p-4 text-sm text-muted">Loading edge analysis…</div>;
  }

  const o = data.overall || {};
  const ml = data.ml_readiness || {};

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-2 sm:p-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["Total trades", o.total_trades ?? 0],
          ["Win rate", pct(o.win_rate)],
          ["Profit factor", o.profit_factor ?? "—"],
          ["Expectancy / trade", money(o.expectancy_per_trade_gbp)],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg border border-border bg-card p-3">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted">{label}</p>
            <p className="mt-1 text-lg font-semibold tabular-nums">{value}</p>
          </div>
        ))}
      </div>

      <Card title="ML readiness">
        <div className="space-y-2">
          <div className="flex justify-between text-[12px]">
            <span>{ml.confirmed_live_trades ?? 0} / {ml.trades_needed_for_ml ?? 50} confirmed live trades</span>
            <span>{ml.percentage_ready ?? 0}%</span>
          </div>
          <div className="h-2 overflow-hidden rounded bg-border">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: `${Math.min(100, ml.percentage_ready ?? 0)}%` }}
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
              <tr className="text-muted">
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
                  <td className="px-2 py-1.5">{row.display_name || row.epic}</td>
                  <td className="px-2 py-1.5 tabular-nums">{row.trades}</td>
                  <td className="px-2 py-1.5 tabular-nums">{pct(row.win_rate)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{row.profit_factor ?? "—"}</td>
                  <td className="px-2 py-1.5 tabular-nums">{money(row.net_pnl_gbp)}</td>
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
              <tr className="text-muted">
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
