import React from "react";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const EXCLUDED_SOURCE_TAGS = ["sim", "soak", "proof", "replay", "test"];
const VALID_RESULTS = new Set(["WIN", "LOSS", "PENDING"]);

function resolvePositions(state) {
  return state?.positions ?? state?.active_trades ?? [];
}

function isExcludedSource(trade) {
  const src = String(
    trade?.source ?? trade?.setup ?? trade?.setup_key ?? "",
  ).toLowerCase();
  return EXCLUDED_SOURCE_TAGS.some((tag) => src.includes(tag));
}

function tradeTimeMs(trade) {
  const ts =
    trade?.closed_at ??
    trade?.closed_time ??
    trade?.time ??
    trade?.ts ??
    trade?.timestamp;
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

function resolveMlDecisionLog(state) {
  const log = state?.ml_decision_log;
  return Array.isArray(log) ? [...log].reverse() : [];
}

function fmtPrice(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(1);
}

function fmtGbp(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n >= 0 ? "+" : "";
  return `${sign}£${n.toFixed(2)}`;
}

function fmtTs(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-GB", {
      dateStyle: "short",
      timeStyle: "medium",
    });
  } catch {
    return String(ts);
  }
}

function fmtLogTs(entry) {
  const ts = entry?.ts ?? entry?.timestamp ?? entry?.time;
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return String(ts);
  }
}

function fmtLogLine(entry) {
  if (entry == null) return "—";
  if (typeof entry === "string") return entry;
  const parts = [
    entry.decision,
    entry.action,
    entry.label,
    entry.setup,
    entry.direction,
  ].filter(Boolean);
  if (parts.length) return parts.join(" · ");
  if (entry.message) return String(entry.message);
  try {
    return JSON.stringify(entry);
  } catch {
    return "—";
  }
}

function fmtBreakEven(pos) {
  if (pos?.break_even != null && !Number.isNaN(Number(pos.break_even))) {
    return fmtPrice(pos.break_even);
  }
  if (pos?.breakeven != null && !Number.isNaN(Number(pos.breakeven))) {
    return fmtPrice(pos.breakeven);
  }
  if (pos?.breakeven_hit != null) return pos.breakeven_hit ? "Hit" : "No";
  if (pos?.break_even_hit != null) return pos.break_even_hit ? "Hit" : "No";
  return "—";
}

function sideMeta(pos) {
  const side = String(pos?.side ?? pos?.direction ?? "").toUpperCase();
  const color =
    side === "BUY"
      ? "text-success"
      : side === "SELL"
        ? "text-danger"
        : "text-foreground";
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

function positionKey(pos, idx) {
  return pos?.deal_id ?? pos?.id ?? `${pos?.epic ?? "row"}-${idx}`;
}

function tradeKey(trade, idx) {
  return trade?.deal_id ?? trade?.id ?? `${trade?.market ?? trade?.epic ?? "row"}-${idx}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Card({ title, children, className = "" }) {
  return (
    <section
      className={[
        "rounded-lg border border-border bg-card p-3 sm:p-4",
        className,
      ].join(" ")}
    >
      {title && <h2 className="label-caps mb-2">{title}</h2>}
      {children}
    </section>
  );
}

function ResultBadge({ result }) {
  const label = String(result ?? "—").toUpperCase();
  return (
    <span
      className={[
        "inline-flex rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase",
        resultBadgeClass(label),
      ].join(" ")}
    >
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// ClosePositionButton — per-row confirm + close
// ---------------------------------------------------------------------------

function ClosePositionButton({ dealId, epic }) {
  const [confirming, setConfirming] = React.useState(false);
  const [loading, setLoading] = React.useState(false);
  const [done, setDone] = React.useState(false);

  if (done) return <span className="text-success text-[10px]">Closed</span>;
  if (loading) return <span className="text-muted text-[10px]">…</span>;
  if (confirming) {
    return (
      <span className="flex gap-1 items-center">
        <button
          className="rounded bg-danger px-1.5 py-0.5 text-[10px] font-semibold text-white"
          onClick={async () => {
            setLoading(true);
            try {
              const endpoint = dealId
                ? `/api/close/${dealId}`
                : `/api/flatten/${encodeURIComponent(epic)}`;
              await fetch(endpoint, { method: "POST" });
              setDone(true);
            } catch (e) {
              console.error(e);
            }
            setLoading(false);
            setConfirming(false);
          }}
        >
          Confirm
        </button>
        <button
          className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted"
          onClick={() => setConfirming(false)}
        >
          Cancel
        </button>
      </span>
    );
  }
  return (
    <button
      className="rounded border border-danger/60 px-1.5 py-0.5 text-[10px] text-danger hover:bg-danger/10"
      onClick={() => setConfirming(true)}
    >
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
        <div className="rounded-lg border border-border bg-card p-6 text-center text-muted">
          Waiting for state…
        </div>
      </div>
    );
  }

  const positions = resolvePositions(state);
  const closedTrades = resolveClosedTrades(state);
  const mlLog = resolveMlDecisionLog(state);

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">
      {/* 1. Active positions */}
      <Card title="Active positions">
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-[11px] sm:text-[12px]">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="px-2 py-1.5 font-normal">Epic</th>
                <th className="px-2 py-1.5 font-normal">Side</th>
                <th className="px-2 py-1.5 font-normal">Entry</th>
                <th className="px-2 py-1.5 font-normal">Current</th>
                <th className="px-2 py-1.5 font-normal">P&amp;L GBP</th>
                <th className="px-2 py-1.5 font-normal">Trail Stop</th>
                <th className="px-2 py-1.5 font-normal">Break-even</th>
                <th className="px-2 py-1.5 font-normal">Time Open</th>
                <th className="px-2 py-1.5 font-normal">Close</th>
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 ? (
                <tr>
                  <td
                    colSpan={9}
                    className="px-2 py-4 text-center text-muted"
                  >
                    No open positions
                  </td>
                </tr>
              ) : (
                positions.map((pos, idx) => {
                  const { side, color: sideColor } = sideMeta(pos);
                  const pnl =
                    pos.pnl_gbp ?? pos.unrealised_pnl_gbp ?? pos.upl;
                  const pnlNum = pnl != null ? Number(pnl) : null;
                  const stop = pos.stop ?? pos.stop_level;
                  const trailLabel =
                    stop != null
                      ? `${fmtPrice(stop)}${pos.trail_active ? " T" : ""}`
                      : "—";
                  const openMins =
                    pos.open_mins ??
                    pos.open_minutes ??
                    pos.time_open_mins ??
                    pos.mins_open;

                  return (
                    <tr
                      key={positionKey(pos, idx)}
                      className="border-b border-border/60 last:border-0"
                    >
                      <td className="px-2 py-2 font-mono text-foreground">
                        {pos.epic || pos.market || "—"}
                      </td>
                      <td className={`px-2 py-2 font-medium ${sideColor}`}>
                        {side || "—"}
                      </td>
                      <td className="px-2 py-2 tabular-nums">
                        {fmtPrice(pos.entry ?? pos.entry_price ?? pos.level)}
                      </td>
                      <td className="px-2 py-2 tabular-nums">
                        {fmtPrice(pos.current ?? pos.mark)}
                      </td>
                      <td
                        className={`px-2 py-2 tabular-nums font-medium ${pnlColor(pnlNum)}`}
                      >
                        {fmtGbp(pnlNum)}
                      </td>
                      <td className="px-2 py-2 tabular-nums">{trailLabel}</td>
                      <td className="px-2 py-2 tabular-nums">
                        {fmtBreakEven(pos)}
                      </td>
                      <td className="px-2 py-2 tabular-nums">
                        {openMins != null
                          ? `${Math.round(Number(openMins))}m`
                          : "—"}
                      </td>
                      <td className="px-2 py-2">
                        <ClosePositionButton
                          dealId={pos.deal_id}
                          epic={pos.epic || pos.market || ""}
                        />
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 2. Closed trades */}
      <Card title="Closed trades">
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[620px] text-left text-[11px] sm:text-[12px]">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="px-2 py-1.5 font-normal">Time</th>
                <th className="px-2 py-1.5 font-normal">Epic</th>
                <th className="px-2 py-1.5 font-normal">Side</th>
                <th className="px-2 py-1.5 font-normal">Entry</th>
                <th className="px-2 py-1.5 font-normal">Exit</th>
                <th className="px-2 py-1.5 font-normal">P&amp;L GBP</th>
                <th className="px-2 py-1.5 font-normal">Result</th>
              </tr>
            </thead>
            <tbody>
              {closedTrades.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-2 py-4 text-center text-muted"
                  >
                    No closed trades this session
                  </td>
                </tr>
              ) : (
                closedTrades.map((trade, idx) => {
                  const { side, color: sideColor } = sideMeta(trade);
                  const pnl = trade.pnl_gbp ?? trade.pnl;
                  const pnlNum = pnl != null ? Number(pnl) : null;
                  const time =
                    trade.closed_at ??
                    trade.closed_time ??
                    trade.time ??
                    trade.ts ??
                    trade.timestamp;

                  return (
                    <tr
                      key={tradeKey(trade, idx)}
                      className="border-b border-border/60 last:border-0"
                    >
                      <td className="px-2 py-2 tabular-nums text-muted">
                        {fmtTs(time)}
                      </td>
                      <td className="px-2 py-2 font-mono text-foreground">
                        {trade.epic ?? trade.market ?? "—"}
                      </td>
                      <td className={`px-2 py-2 font-medium ${sideColor}`}>
                        {side || "—"}
                      </td>
                      <td className="px-2 py-2 tabular-nums">
                        {fmtPrice(trade.entry ?? trade.entry_price)}
                      </td>
                      <td className="px-2 py-2 tabular-nums">
                        {fmtPrice(trade.exit ?? trade.exit_price)}
                      </td>
                      <td
                        className={`px-2 py-2 tabular-nums font-medium ${pnlColor(pnlNum)}`}
                      >
                        {fmtGbp(pnlNum)}
                      </td>
                      <td className="px-2 py-2">
                        <ResultBadge result={trade.result} />
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 3. ML decision log */}
      <Card title="ML decision log">
        <div className="h-[200px] overflow-y-auto rounded border border-border/60 bg-bg/50">
          {mlLog.length === 0 ? (
            <p className="px-2 py-3 text-center font-mono text-[11px] text-muted">
              No signals this session
            </p>
          ) : (
            <ul className="divide-y divide-border/60">
              {mlLog.map((entry, idx) => (
                <li
                  key={entry?.id ?? idx}
                  className="flex flex-wrap items-baseline gap-x-2 px-2 py-1.5 font-mono text-[11px]"
                >
                  <span className="shrink-0 tabular-nums text-muted">
                    {fmtLogTs(entry)}
                  </span>
                  <span className="min-w-0 flex-1 text-foreground">
                    {fmtLogLine(entry)}
                  </span>
                  {entry?.confidence != null && (
                    <span className="shrink-0 tabular-nums text-accent">
                      {Number(entry.confidence) <= 1
                        ? Number(entry.confidence).toFixed(2)
                        : `${Math.round(Number(entry.confidence))}%`}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </Card>
    </div>
  );
}
