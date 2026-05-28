function isNil(v) {
  return v == null || v === "";
}

function dash(v, formatter) {
  if (isNil(v) || (typeof v === "number" && Number.isNaN(v))) return "—";
  return formatter ? formatter(v) : v;
}

function fmtPrice(v) {
  return Number(v).toFixed(1);
}

function fmtPoints(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return String(Math.round(n));
}

function fmtFitness(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return String(Math.round(n));
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
      return {
        label: "HEALTHY",
        className: "border-success/40 bg-success/10 text-success",
        flash: false,
      };
    case "CAUTION":
      return {
        label: "CAUTION",
        className: "border-warning/40 bg-warning/10 text-warning",
        flash: false,
      };
    case "WARNING":
      return {
        label: "WARNING",
        className: "border-warning/40 bg-warning/10 text-warning",
        flash: false,
      };
    case "DANGER":
      return {
        label: "DANGER",
        className: "border-danger/40 bg-danger/10 text-danger",
        flash: false,
      };
    case "STOP":
      return {
        label: "STOP",
        className: "border-danger/40 bg-danger/10 text-danger",
        flash: true,
      };
    default:
      return {
        label: isNil(state) ? "—" : s,
        className: "border-border bg-card text-muted",
        flash: false,
      };
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

function resolveStreamStatus(streamStatus, wsConnected, reconnecting) {
  if (!wsConnected || reconnecting) return "DISCONNECTED";
  const s = String(streamStatus ?? "").toUpperCase();
  if (s === "LIVE" || s === "STALE" || s === "DISCONNECTED") return s;
  return isNil(streamStatus) ? "DISCONNECTED" : s;
}

function streamStyle(status) {
  switch (status) {
    case "LIVE":
      return { dot: "bg-success", text: "text-success", pulse: true };
    case "STALE":
      return { dot: "bg-warning", text: "text-warning", pulse: false };
    case "DISCONNECTED":
    default:
      return { dot: "bg-danger", text: "text-danger", pulse: false };
  }
}

function sentimentBadge(sentiment) {
  const s = String(sentiment ?? "").toLowerCase();
  if (s === "crowded_long") return { label: "↑CL", show: true };
  if (s === "crowded_short") return { label: "↓CS", show: true };
  return { label: null, show: false };
}

function Dot({ className, pulse }) {
  return (
    <span
      className={[
        "inline-block h-1.5 w-1.5 shrink-0 rounded-full",
        className,
        pulse ? "animate-pulse" : "",
      ].join(" ")}
      aria-hidden
    />
  );
}

function Stat({ label, value, valueClassName = "text-foreground" }) {
  return (
    <div className="flex flex-col items-center leading-none">
      <span className="label-caps">{label}</span>
      <span className={`mt-0.5 text-[11px] font-medium tabular-nums ${valueClassName}`}>
        {value}
      </span>
    </div>
  );
}

export default function Header({
  bid,
  offer,
  agentState,
  pointsTrade,
  pointsSession,
  pointsCumulative,
  fitness,
  winRate,
  dailyPnl,
  streamStatus,
  spreadCurrent,
  spreadNormal,
  sentiment,
  wsConnected,
  reconnecting,
}) {
  const agent = agentStateStyle(agentState);
  const stream = resolveStreamStatus(streamStatus, wsConnected, reconnecting);
  const streamUi = streamStyle(stream);
  const spreadRatio =
    !isNil(spreadCurrent) &&
    !isNil(spreadNormal) &&
    Number(spreadNormal) > 0
      ? Number(spreadCurrent) / Number(spreadNormal)
      : null;
  const spreadColor = spreadTextColor(spreadCurrent, spreadNormal);
  const pnlColor =
    isNil(dailyPnl) || Number.isNaN(Number(dailyPnl))
      ? "text-foreground"
      : Number(dailyPnl) >= 0
        ? "text-success"
        : "text-danger";
  const sent = sentimentBadge(sentiment);

  return (
    <header className="sticky top-0 z-20 shrink-0 border-b border-border bg-bg px-2 py-2 sm:px-3">
      <div className="flex min-w-0 flex-nowrap items-center gap-x-3 gap-y-2 max-[390px]:flex-wrap">
        <span
          className={[
            "inline-flex shrink-0 items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide",
            agent.className,
            agent.flash ? "animate-pulse" : "",
          ].join(" ")}
        >
          {agent.label}
        </span>

        <div className="flex shrink-0 items-baseline gap-2 font-mono text-price leading-none">
          <span className="text-muted">
            <span className="label-caps mr-1 font-sans">Bid</span>
            <span className="text-foreground">{dash(bid, fmtPrice)}</span>
          </span>
          <span className="text-border">|</span>
          <span className="text-muted">
            <span className="label-caps mr-1 font-sans">Offer</span>
            <span className="text-foreground">{dash(offer, fmtPrice)}</span>
          </span>
        </div>

        <div
          className={`inline-flex shrink-0 items-center gap-1.5 text-[11px] font-medium ${streamUi.text}`}
        >
          <Dot className={streamUi.dot} pulse={streamUi.pulse} />
          <span className="label-caps text-muted">Stream</span>
          <span>{stream}</span>
        </div>

        <div className={`flex shrink-0 items-baseline gap-1 text-[11px] ${spreadColor}`}>
          <span className="label-caps text-muted">Spread</span>
          <span className="font-medium tabular-nums">{fmtSpread(spreadCurrent)}</span>
          {spreadRatio != null && (
            <span className="text-[10px] tabular-nums text-muted">
              ({spreadRatio.toFixed(1)}×)
            </span>
          )}
        </div>

        {sent.show && (
          <span className="inline-flex shrink-0 items-center rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[11px] font-semibold text-warning">
            {sent.label}
          </span>
        )}

        <div className={`flex shrink-0 items-baseline gap-1 text-[11px] ${pnlColor}`}>
          <span className="label-caps text-muted">Daily P&amp;L</span>
          <span className="font-medium tabular-nums">{dash(dailyPnl, fmtPnl)}</span>
        </div>

        <div className="flex shrink-0 items-center gap-2 rounded-md border border-border bg-card px-2 py-1">
          <Stat label="T" value={fmtPoints(pointsTrade)} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Stat label="S" value={fmtPoints(pointsSession)} />
          <span className="h-4 w-px bg-border" aria-hidden />
          <Stat label="C" value={fmtPoints(pointsCumulative)} />
        </div>

        <Stat
          label="Fit"
          value={fmtFitness(fitness)}
          valueClassName={fitnessColor(fitness)}
        />

        <Stat label="WR" value={dash(winRate, fmtWinRate)} />
      </div>
    </header>
  );
}
