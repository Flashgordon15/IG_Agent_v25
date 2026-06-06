import { fmtPrice } from "../utils/fmtPrice.js";

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
  return `${sign}${Math.round(n)}`;
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
  const tradingStopped =
    tradingLoopsRunning === false || tradingPaused === true;
  const agent = tradingStopped
    ? { label: "TRADING STOPPED", className: "border-danger/40 bg-danger/10 text-danger", flash: true }
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
          <Pill label="Daily P&L" value={dash(dailyPnl, fmtPnl)} valueClassName={pnlColor} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Pill label="Win Rate" value={dash(winRate, fmtWinRate)} />
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

        {/* Stop Agent button — always visible, right-most */}
        <div className="ml-auto shrink-0">
          <button
            type="button"
            onClick={onStopAgent}
            className="inline-flex items-center gap-1.5 rounded-md border border-danger/50 bg-danger/10 px-2.5 py-1 text-[11px] font-semibold text-danger transition-colors hover:bg-danger/20 active:scale-95"
            title="Save session state and shut down the agent cleanly"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden />
            Stop Agent
          </button>
        </div>
      </div>
    </header>
  );
}
