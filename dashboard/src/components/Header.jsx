import { useCallback, useState } from "react";
import { fmtPrice } from "../utils/fmtPrice.js";
import StrategyHelpModal from "./StrategyHelpModal.jsx";

function isNil(v) {
  return v == null || v === "";
}

function dash(v, formatter, epic) {
  if (isNil(v) || (typeof v === "number" && Number.isNaN(v))) return "—";
  return formatter ? formatter(v, epic) : v;
}

function fmtPoints(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}`;
}

function fmtPnl(v) {
  const n = Number(v);
  const sign = n >= 0 ? "+" : "";
  return `${sign}£${n.toFixed(2)}`;
}

function fmtWinRate(v) {
  return `${Math.round(Number(v))}%`;
}

function fmtSpread(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(1);
}

function agentStateStyle(state) {
  const s = String(state ?? "").toUpperCase();
  switch (s) {
    case "HEALTHY":
      return { label: "HEALTHY", className: "border-success/40 bg-success/10 text-success", flash: false };
    case "CAUTION":
      return { label: "CAUTION", className: "border-warning/40 bg-warning/10 text-warning", flash: false };
    case "WARNING":
      return { label: "WARNING", className: "border-warning/40 bg-warning/10 text-warning", flash: false };
    case "DANGER":
      return { label: "DANGER", className: "border-danger/40 bg-danger/10 text-danger", flash: false };
    case "STOP":
      return { label: "STOP", className: "border-danger/40 bg-danger/10 text-danger", flash: true };
    default:
      return { label: isNil(state) ? "—" : s, className: "border-border bg-card text-muted", flash: false };
  }
}

function fitnessColor(v) {
  if (isNil(v) || Number.isNaN(Number(v))) return "text-foreground";
  const n = Number(v);
  if (n >= 60) return "text-success";
  if (n >= 40) return "text-warning";
  return "text-danger";
}

function spreadTextColor(current, normal) {
  if (isNil(current) || isNil(normal) || Number(normal) <= 0) return "text-foreground";
  const ratio = Number(current) / Number(normal);
  if (ratio > 2) return "text-danger";
  if (ratio > 1.5) return "text-warning";
  return "text-foreground";
}

function resolveStreamStatus(streamStatus, wsConnected, reconnecting, marketState) {
  if (!wsConnected || reconnecting) return "DISCONNECTED";
  const ms = String(marketState ?? "").toUpperCase();
  // When the market itself is closed or in maintenance, show that state rather
  // than the Lightstreamer stream health — the boxes are the source of truth.
  if (ms === "MAINTENANCE") return "MAINTENANCE";
  if (ms === "CLOSED") return "CLOSED";
  const s = String(streamStatus ?? "").toUpperCase();
  if (s === "LIVE" || s === "STALE" || s === "DISCONNECTED") return s;
  return isNil(streamStatus) ? "DISCONNECTED" : s;
}

function streamStyle(status) {
  switch (status) {
    case "LIVE":        return { dot: "bg-success",  text: "text-success",  pulse: true,  label: "LIVE" };
    case "STALE":       return { dot: "bg-warning",  text: "text-warning",  pulse: false, label: "STALE" };
    case "MAINTENANCE": return { dot: "bg-danger",   text: "text-danger",   pulse: false, label: "MAINT" };
    case "CLOSED":      return { dot: "bg-muted",    text: "text-muted",    pulse: false, label: "CLOSED" };
    default:            return { dot: "bg-danger",   text: "text-danger",   pulse: false, label: status };
  }
}

function Dot({ className, pulse }) {
  return (
    <span
      className={["inline-block h-2 w-2 shrink-0 rounded-full", className, pulse ? "animate-pulse" : ""].join(" ")}
      aria-hidden
    />
  );
}

function Pill({ label, value, valueClassName = "text-foreground" }) {
  return (
    <div className="flex flex-col items-center min-w-0">
      <span className="text-[9px] font-medium uppercase tracking-widest text-muted/70">{label}</span>
      <span className={`text-[11px] font-semibold tabular-nums leading-none mt-0.5 ${valueClassName}`}>
        {value}
      </span>
    </div>
  );
}

export default function Header({
  bid,
  offer,
  epic,
  agentState,
  tradingLoopsRunning,
  tradingPaused,
  tradingHealthy,
  agentAlive,
  pointsTrade,
  pointsSession,
  pointsCumulative,
  fitness,
  winRate,
  dailyPnl,
  streamStatus,
  marketState,
  spreadCurrent,
  spreadNormal,
  sentiment,
  wsConnected,
  reconnecting,
  openPositions,
  maxPositions,
  onStopAgent,
}) {
  const [safeLeaveModal, setSafeLeaveModal] = useState(null);
  const [helpOpen, setHelpOpen] = useState(false);

  const handleSafeToLeave = useCallback(async () => {
    setSafeLeaveModal({ loading: true });
    try {
      const res = await fetch("/api/safe-to-leave", { method: "POST" });
      const text = await res.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        throw new Error(
          res.ok ? text.slice(0, 120) : `HTTP ${res.status}: ${text.slice(0, 120)}`,
        );
      }
      setSafeLeaveModal({ loading: false, ...data });
    } catch (e) {
      setSafeLeaveModal({
        loading: false,
        ok: false,
        message: "Request failed",
        checks: [],
        error: String(e?.message || e),
      });
    }
  }, []);

  const tradingStopped =
    agentAlive === false
    || tradingLoopsRunning === false
    || tradingPaused === true
    || tradingHealthy === false;
  const agent = agentAlive === false
    ? {
        label: "AGENT OFFLINE",
        className: "border-danger/40 bg-danger/10 text-danger",
        flash: true,
      }
    : tradingStopped
      ? {
          label: tradingLoopsRunning === false || tradingPaused
            ? "TRADING STOPPED"
            : "NOT TRADING",
          className: "border-danger/40 bg-danger/10 text-danger",
          flash: true,
        }
      : agentStateStyle(agentState);
  const stream = resolveStreamStatus(streamStatus, wsConnected, reconnecting, marketState);
  const streamUi = streamStyle(stream);
  const spreadRatio =
    !isNil(spreadCurrent) && !isNil(spreadNormal) && Number(spreadNormal) > 0
      ? Number(spreadCurrent) / Number(spreadNormal)
      : null;
  const spreadColor = spreadTextColor(spreadCurrent, spreadNormal);
  const pnlColor =
    isNil(dailyPnl) || Number.isNaN(Number(dailyPnl))
      ? "text-foreground"
      : Number(dailyPnl) >= 0 ? "text-success" : "text-danger";

  const posOpen = openPositions ?? 0;
  const posMax = maxPositions ?? 10;
  const posCapacityColor = posOpen >= posMax ? "text-danger" : posOpen >= posMax * 0.8 ? "text-warning" : "text-foreground";

  return (
    <header className="sticky top-0 z-20 shrink-0 border-b border-border bg-bg/95 backdrop-blur-sm px-3 py-2">
      <div className="flex min-w-0 items-center gap-x-2 gap-y-1.5 flex-wrap">

        {/* Agent state badge */}
        <span
          className={[
            "inline-flex shrink-0 items-center rounded-md border px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider",
            agent.className,
            agent.flash ? "animate-pulse" : "",
          ].join(" ")}
        >
          {agent.label}
        </span>

        {/* Divider */}
        <span className="hidden sm:block h-5 w-px bg-border shrink-0" aria-hidden />

        {/* Stream / market status */}
        <div className={`inline-flex shrink-0 items-center gap-1.5 text-[11px] ${streamUi.text}`}>
          <Dot className={streamUi.dot} pulse={streamUi.pulse} />
          <span className="font-medium">{streamUi.label}</span>
        </div>

        {/* Bid/Offer */}
        <div className="flex shrink-0 items-center gap-1 font-mono">
          <span className="label-caps text-muted">B</span>
          <span className="text-[13px] font-semibold tabular-nums text-foreground">
            {dash(bid, fmtPrice, epic)}
          </span>
          <span className="text-border mx-0.5">/</span>
          <span className="label-caps text-muted">O</span>
          <span className="text-[13px] font-semibold tabular-nums text-foreground">
            {dash(offer, fmtPrice, epic)}
          </span>
        </div>

        {/* Spread */}
        <div className={`flex shrink-0 items-center gap-1 text-[11px] ${spreadColor}`}>
          <span className="label-caps text-muted">Spd</span>
          <span className="font-medium tabular-nums">{fmtSpread(spreadCurrent)}</span>
          {spreadRatio != null && (
            <span className="text-[10px] text-muted">({spreadRatio.toFixed(1)}×)</span>
          )}
        </div>

        {/* Divider */}
        <span className="hidden sm:block h-5 w-px bg-border shrink-0" aria-hidden />

        {/* Stat pills group */}
        <div className="flex items-center gap-3 rounded-lg border border-border bg-card/60 px-3 py-1.5">
          <Pill label="Today P&L" value={dash(dailyPnl, fmtPnl)} valueClassName={pnlColor} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Pill label="Win (last 20)" value={dash(winRate, fmtWinRate)} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Pill label="Fit" value={isNil(fitness) ? "—" : String(Math.round(Number(fitness)))} valueClassName={fitnessColor(fitness)} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Pill label="Pos" value={`${posOpen}/${posMax}`} valueClassName={posCapacityColor} />
        </div>

        {/* Points group */}
        <div className="flex items-center gap-3 rounded-lg border border-border bg-card/60 px-3 py-1.5">
          <Pill label="Trade" value={fmtPoints(pointsTrade)} valueClassName={Number(pointsTrade) > 0 ? "text-success" : Number(pointsTrade) < 0 ? "text-danger" : "text-foreground"} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Pill label="Session" value={fmtPoints(pointsSession)} valueClassName={Number(pointsSession) > 0 ? "text-success" : Number(pointsSession) < 0 ? "text-danger" : "text-foreground"} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Pill label="Cumul." value={fmtPoints(pointsCumulative)} valueClassName={Number(pointsCumulative) > 0 ? "text-success" : Number(pointsCumulative) < 0 ? "text-danger" : "text-foreground"} />
        </div>

        {/* Sentiment badge */}
        {sentiment && (() => {
          const lbl = typeof sentiment === "object" ? sentiment?.label : sentiment;
          return (lbl === "crowded_long" || lbl === "crowded_short") ? (
            <span className="inline-flex shrink-0 items-center rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[10px] font-semibold text-warning">
              {lbl === "crowded_long" ? "↑ CROWD LONG" : "↓ CROWD SHORT"}
            </span>
          ) : null;
        })()}

        {/* Help + Safe to leave + Stop Agent — right-most */}
        <div className="ml-auto flex shrink-0 flex-col items-stretch gap-1">
          <button
            type="button"
            onClick={() => setHelpOpen(true)}
            className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border bg-card/80 px-2.5 py-1 text-[11px] font-semibold text-foreground transition-colors hover:bg-card active:scale-95"
            title="Strategy, gates, sizing, points & ML reference"
          >
            <span className="text-[12px] leading-none" aria-hidden>?</span>
            Strategy help
          </button>
          <button
            type="button"
            onClick={handleSafeToLeave}
            disabled={safeLeaveModal?.loading}
            className="inline-flex items-center justify-center gap-1.5 rounded-md border border-success/50 bg-success/10 px-2.5 py-1 text-[11px] font-semibold text-success transition-colors hover:bg-success/20 active:scale-95 disabled:cursor-wait disabled:opacity-60"
            title="Run overnight trust checks before walking away"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-success" aria-hidden />
            {safeLeaveModal?.loading ? "Checking…" : "Safe to Leave"}
          </button>
          <button
            type="button"
            onClick={onStopAgent}
            className="inline-flex items-center justify-center gap-1.5 rounded-md border border-danger/50 bg-danger/10 px-2.5 py-1 text-[11px] font-semibold text-danger transition-colors hover:bg-danger/20 active:scale-95"
            title="Save session state and shut down the agent cleanly"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden />
            Stop Agent
          </button>
        </div>
      </div>

      <StrategyHelpModal open={helpOpen} onClose={() => setHelpOpen(false)} />

      {safeLeaveModal && !safeLeaveModal.loading && (
        <div
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-labelledby="safe-to-leave-title"
        >
          <div className="mx-4 flex max-h-[80vh] w-full max-w-md flex-col overflow-hidden rounded-xl border border-border bg-card p-6 shadow-xl">
            <h2
              id="safe-to-leave-title"
              className={`text-center text-[15px] font-semibold ${safeLeaveModal.ok ? "text-success" : "text-danger"}`}
            >
              {safeLeaveModal.ok ? "Safe to Leave" : "Not Safe to Leave"}
            </h2>
            {safeLeaveModal.message && (
              <p className="mt-2 text-center text-[12px] text-muted">{safeLeaveModal.message}</p>
            )}
            {safeLeaveModal.error && (
              <p className="mt-2 text-center text-[12px] text-danger">{safeLeaveModal.error}</p>
            )}
            <ul className="mt-4 max-h-[50vh] space-y-1 overflow-y-auto text-left text-[11px]">
              {(safeLeaveModal.checks || []).map((row) => (
                <li
                  key={`${row.status}-${row.label}`}
                  className={
                    row.status === "pass"
                      ? "text-success"
                      : row.status === "skip"
                        ? "text-muted"
                        : "text-danger"
                  }
                >
                  [{row.status.toUpperCase()}] {row.label}
                  {row.detail ? ` — ${row.detail}` : ""}
                </li>
              ))}
            </ul>
            <button
              type="button"
              onClick={() => setSafeLeaveModal(null)}
              className="mt-5 rounded-md border border-border px-4 py-2 text-[12px] font-semibold text-muted transition-colors hover:bg-card/80"
            >
              Close
            </button>
          </div>
        </div>
      )}
    </header>
  );
}
