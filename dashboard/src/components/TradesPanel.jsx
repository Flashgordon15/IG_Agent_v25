import React from "react";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const EXCLUDED_SOURCE_TAGS = ["sim", "soak", "proof", "replay", "test"];
const VALID_RESULTS = new Set(["WIN", "LOSS", "PENDING"]);

function resolvePositions(state) {
  // Top-level positions (aggregated by backend from all market slices)
  if (Array.isArray(state?.positions) && state.positions.length > 0) return state.positions;
  if (Array.isArray(state?.active_trades) && state.active_trades.length > 0) return state.active_trades;
  // Fallback: aggregate from per-market slices in case backend hasn't enriched yet
  const markets = state?.markets;
  if (markets && typeof markets === "object") {
    const all = [];
    for (const [epic, mslice] of Object.entries(markets)) {
      const positions = mslice?.positions;
      if (Array.isArray(positions)) {
        positions.forEach((p) => {
          all.push({ epic, market: mslice?.market_name ?? mslice?.market ?? epic, ...p });
        });
      }
    }
    if (all.length > 0) return all;
  }
  return [];
}

function isExcludedSource(trade) {
  const src = String(trade?.source ?? trade?.setup ?? trade?.setup_key ?? "").toLowerCase();
  return EXCLUDED_SOURCE_TAGS.some((tag) => src.includes(tag));
}

function tradeTimeMs(trade) {
  const ts = trade?.closed_at ?? trade?.closed_time ?? trade?.time ?? trade?.ts ?? trade?.timestamp;
  if (!ts) return 0;
  const ms = Date.parse(ts);
  return Number.isNaN(ms) ? 0 : ms;
}

function resolveClosedTrades(state) {
  const raw = state?.closed_trades;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((trade) => {
      const result = String(trade?.result ?? "").toUpperCase();
      if (!VALID_RESULTS.has(result)) return false;
      if (isExcludedSource(trade)) return false;
      return true;
    })
    .sort((a, b) => tradeTimeMs(b) - tradeTimeMs(a))
    .slice(0, 100);
}

function fmtGbp(v, opts = {}) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n >= 0 ? "+" : "";
  return opts.abs ? `£${Math.abs(n).toFixed(2)}` : `${sign}£${n.toFixed(2)}`;
}

function fmtTs(ts) {
  if (!ts) return "—";
  try { return new Date(ts).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "medium" }); }
  catch { return String(ts); }
}

function fmtPrice(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(1);
}

function sideMeta(pos) {
  const side = String(pos?.side ?? pos?.direction ?? "").toUpperCase();
  const color = side === "BUY" ? "text-success" : side === "SELL" ? "text-danger" : "text-foreground";
  return { side, color };
}

function pnlColor(v) {
  if (v == null || Number.isNaN(Number(v))) return "text-foreground";
  return Number(v) >= 0 ? "text-success" : "text-danger";
}

function resultBadgeClass(result) {
  const r = String(result ?? "").toUpperCase();
  if (r === "WIN") return "border-success/40 bg-success/10 text-success";
  if (r === "LOSS") return "border-danger/40 bg-danger/10 text-danger";
  return "border-warning/40 bg-warning/10 text-warning";
}

function positionKey(pos, idx) { return pos?.deal_id ?? pos?.id ?? `${pos?.epic ?? "row"}-${idx}`; }
function tradeKey(trade, idx) { return trade?.deal_id ?? trade?.id ?? `${trade?.market ?? trade?.epic ?? "row"}-${idx}`; }

function fmtBreakEven(pos) {
  if (pos?.break_even != null && !Number.isNaN(Number(pos.break_even))) return fmtPrice(pos.break_even);
  if (pos?.breakeven   != null && !Number.isNaN(Number(pos.breakeven)))  return fmtPrice(pos.breakeven);
  if (pos?.breakeven_hit != null) return pos.breakeven_hit ? "Hit" : "No";
  if (pos?.break_even_hit != null) return pos.break_even_hit ? "Hit" : "No";
  return "—";
}

// ---------------------------------------------------------------------------
// Performance summary helpers
// ---------------------------------------------------------------------------

function buildPerformanceSummary(closedTrades, positions) {
  const wins   = closedTrades.filter((t) => String(t.result ?? "").toUpperCase() === "WIN").length;
  const losses = closedTrades.filter((t) => String(t.result ?? "").toUpperCase() === "LOSS").length;
  const total  = wins + losses;
  const winRate = total > 0 ? Math.round((wins / total) * 100) : null;
  const totalPnl = closedTrades.reduce((acc, t) => acc + (Number(t.pnl_gbp ?? t.pnl) || 0), 0);
  const openPnl  = positions.reduce((acc, p) => acc + (Number(p.pnl_gbp ?? p.unrealised_pnl_gbp ?? p.upl) || 0), 0);
  return { wins, losses, total, winRate, totalPnl, openPnl };
}

function buildMarketBreakdown(closedTrades) {
  const map = {};
  for (const t of closedTrades) {
    const key = t.market || t.epic || "Unknown";
    if (!map[key]) map[key] = { wins: 0, losses: 0, pnl: 0 };
    const result = String(t.result ?? "").toUpperCase();
    if (result === "WIN") map[key].wins++;
    else if (result === "LOSS") map[key].losses++;
    map[key].pnl += Number(t.pnl_gbp ?? t.pnl) || 0;
  }
  return Object.entries(map)
    .sort(([, a], [, b]) => (b.wins + b.losses) - (a.wins + a.losses))
    .slice(0, 8);
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Card({ title, children, className = "" }) {
  return (
    <section className={["rounded-lg border border-border bg-card p-3 sm:p-4", className].join(" ")}>
      {title && <h2 className="label-caps mb-2">{title}</h2>}
      {children}
    </section>
  );
}

function ResultBadge({ result }) {
  const label = String(result ?? "—").toUpperCase();
  return (
    <span className={["inline-flex rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase", resultBadgeClass(label)].join(" ")}>
      {label}
    </span>
  );
}

function StatBox({ label, value, valueClassName = "text-foreground" }) {
  return (
    <div className="flex flex-col items-center rounded-lg border border-border bg-surface p-3">
      <span className="label-caps">{label}</span>
      <span className={`mt-1 text-xl font-bold tabular-nums leading-none ${valueClassName}`}>{value}</span>
    </div>
  );
}

function ClosePositionButton({ dealId, epic }) {
  const [confirming, setConfirming] = React.useState(false);
  const [loading,    setLoading]    = React.useState(false);
  const [done,       setDone]       = React.useState(false);
  if (done)    return <span className="text-success text-[10px]">Closed</span>;
  if (loading) return <span className="text-muted text-[10px]">…</span>;
  if (confirming) {
    return (
      <span className="flex gap-1 items-center">
        <button className="rounded bg-danger px-1.5 py-0.5 text-[10px] font-semibold text-white"
          onClick={async () => {
            setLoading(true);
            try {
              const endpoint = dealId ? `/api/close/${dealId}` : `/api/flatten/${encodeURIComponent(epic)}`;
              await fetch(endpoint, { method: "POST" });
              setDone(true);
            } catch (e) { console.error(e); }
            setLoading(false); setConfirming(false);
          }}>Confirm</button>
        <button className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted" onClick={() => setConfirming(false)}>Cancel</button>
      </span>
    );
  }
  return (
    <button className="rounded border border-danger/60 px-1.5 py-0.5 text-[10px] text-danger hover:bg-danger/10" onClick={() => setConfirming(true)}>
      Close
    </button>
  );
}

// ---------------------------------------------------------------------------
// TradesPanel
// ---------------------------------------------------------------------------

export default function TradesPanel({ state }) {
  if (!state) {
    return (
      <div className="mx-auto max-w-5xl space-y-3 px-1">
        <div className="rounded-lg border border-border bg-card p-6 text-center text-muted">Waiting for state…</div>
      </div>
    );
  }

  const positions    = resolvePositions(state);
  const closedTrades = resolveClosedTrades(state);
  const { wins, losses, total, winRate, totalPnl, openPnl } = buildPerformanceSummary(closedTrades, positions);
  const marketBreakdown = buildMarketBreakdown(closedTrades);

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">

      {/* 1. Performance summary stats */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 sm:gap-3">
        <StatBox label="Closed P&L" value={total > 0 ? fmtGbp(totalPnl) : "—"} valueClassName={totalPnl >= 0 ? "text-success" : "text-danger"} />
        <StatBox label="Open P&L"   value={positions.length > 0 ? fmtGbp(openPnl) : "—"} valueClassName={openPnl >= 0 ? "text-success" : "text-danger"} />
        <StatBox label="Win rate"   value={winRate != null ? `${winRate}%` : "—"} valueClassName={winRate != null && winRate >= 50 ? "text-success" : "text-warning"} />
        <StatBox label="Trades"     value={`${wins}W / ${losses}L`} valueClassName="text-foreground" />
      </div>

      {/* 2. Per-market breakdown */}
      {marketBreakdown.length > 0 && (
        <Card title="Market breakdown">
          <div className="-mx-1 overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead>
                <tr className="border-b border-border text-muted">
                  <th className="px-2 py-1.5 font-normal">Market</th>
                  <th className="px-2 py-1.5 font-normal text-right">Trades</th>
                  <th className="px-2 py-1.5 font-normal text-right">W / L</th>
                  <th className="px-2 py-1.5 font-normal text-right">Win %</th>
                  <th className="px-2 py-1.5 font-normal text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {marketBreakdown.map(([market, d]) => {
                  const t = d.wins + d.losses;
                  const wr = t > 0 ? Math.round((d.wins / t) * 100) : null;
                  return (
                    <tr key={market} className="border-b border-border/60 last:border-0">
                      <td className="px-2 py-1.5 font-medium text-foreground">{market}</td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-muted">{t}</td>
                      <td className="px-2 py-1.5 text-right tabular-nums">
                        <span className="text-success">{d.wins}</span>
                        <span className="text-muted"> / </span>
                        <span className="text-danger">{d.losses}</span>
                      </td>
                      <td className={`px-2 py-1.5 text-right tabular-nums font-medium ${wr != null && wr >= 50 ? "text-success" : "text-warning"}`}>
                        {wr != null ? `${wr}%` : "—"}
                      </td>
                      <td className={`px-2 py-1.5 text-right tabular-nums font-medium ${d.pnl >= 0 ? "text-success" : "text-danger"}`}>
                        {fmtGbp(d.pnl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* 3. Active positions */}
      <Card title="Active positions">
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-[11px] sm:text-[12px]">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="px-2 py-1.5 font-normal">Market</th>
                <th className="px-2 py-1.5 font-normal">Side</th>
                <th className="px-2 py-1.5 font-normal">Entry</th>
                <th className="px-2 py-1.5 font-normal">Current</th>
                <th className="px-2 py-1.5 font-normal">P&amp;L</th>
                <th className="px-2 py-1.5 font-normal">Stop</th>
                <th className="px-2 py-1.5 font-normal">B/E</th>
                <th className="px-2 py-1.5 font-normal">Open</th>
                <th className="px-2 py-1.5 font-normal">×</th>
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 ? (
                <tr><td colSpan={9} className="px-2 py-4 text-center text-muted">No open positions</td></tr>
              ) : (
                positions.map((pos, idx) => {
                  const { side, color: sideColor } = sideMeta(pos);
                  const pnl = pos.pnl_gbp ?? pos.unrealised_pnl_gbp ?? pos.upl;
                  const pnlNum = pnl != null ? Number(pnl) : null;
                  const stop = pos.stop ?? pos.stop_level;
                  const openMins = pos.open_mins ?? pos.open_minutes ?? pos.time_open_mins ?? pos.mins_open;
                  return (
                    <tr key={positionKey(pos, idx)} className="border-b border-border/60 last:border-0 hover:bg-card/50 transition-colors">
                      <td className="px-2 py-2 font-medium text-foreground">{pos.market || pos.epic || "—"}</td>
                      <td className={`px-2 py-2 font-bold ${sideColor}`}>{side || "—"}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(pos.entry ?? pos.entry_price ?? pos.level)}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(pos.current ?? pos.mark)}</td>
                      <td className={`px-2 py-2 font-mono tabular-nums font-semibold ${pnlColor(pnlNum)}`}>{fmtGbp(pnlNum)}</td>
                      <td className="px-2 py-2 font-mono tabular-nums text-muted">{stop != null ? `${fmtPrice(stop)}${pos.trail_active ? " ↕" : ""}` : "—"}</td>
                      <td className="px-2 py-2 tabular-nums text-muted">{fmtBreakEven(pos)}</td>
                      <td className="px-2 py-2 tabular-nums text-muted">{openMins != null ? `${Math.round(Number(openMins))}m` : "—"}</td>
                      <td className="px-2 py-2"><ClosePositionButton dealId={pos.deal_id} epic={pos.epic || pos.market || ""} /></td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 4. Closed trades */}
      <Card title="Closed trades">
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[620px] text-left text-[11px] sm:text-[12px]">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="px-2 py-1.5 font-normal">Time</th>
                <th className="px-2 py-1.5 font-normal">Market</th>
                <th className="px-2 py-1.5 font-normal">Side</th>
                <th className="px-2 py-1.5 font-normal">Entry</th>
                <th className="px-2 py-1.5 font-normal">Exit</th>
                <th className="px-2 py-1.5 font-normal">P&amp;L</th>
                <th className="px-2 py-1.5 font-normal">Result</th>
              </tr>
            </thead>
            <tbody>
              {closedTrades.length === 0 ? (
                <tr><td colSpan={7} className="px-2 py-4 text-center text-muted">No closed trades this session</td></tr>
              ) : (
                closedTrades.map((trade, idx) => {
                  const { side, color: sideColor } = sideMeta(trade);
                  const pnl = trade.pnl_gbp ?? trade.pnl;
                  const pnlNum = pnl != null ? Number(pnl) : null;
                  const time = trade.closed_at ?? trade.closed_time ?? trade.time ?? trade.ts ?? trade.timestamp;
                  return (
                    <tr key={tradeKey(trade, idx)} className="border-b border-border/60 last:border-0 hover:bg-card/50 transition-colors">
                      <td className="px-2 py-2 tabular-nums text-muted">{fmtTs(time)}</td>
                      <td className="px-2 py-2 font-medium text-foreground">{trade.epic ?? trade.market ?? "—"}</td>
                      <td className={`px-2 py-2 font-bold ${sideColor}`}>{side || "—"}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(trade.entry ?? trade.entry_price)}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(trade.exit ?? trade.exit_price)}</td>
                      <td className={`px-2 py-2 font-mono tabular-nums font-semibold ${pnlColor(pnlNum)}`}>{fmtGbp(pnlNum)}</td>
                      <td className="px-2 py-2"><ResultBadge result={trade.result} /></td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
