import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isNil(v) {
  return v == null || v === "";
}

function fmtPoints(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}`;
}

function pointsColor(v) {
  if (isNil(v) || Number.isNaN(Number(v))) return "text-foreground";
  const n = Number(v);
  if (n > 0) return "text-success";
  if (n < 0) return "text-danger";
  return "text-foreground";
}

function resolveAgentState(state) {
  return state?.agent_state ?? state?.points?.state ?? null;
}

function agentStateMeta(stateName) {
  const s = String(stateName ?? "").toUpperCase();
  switch (s) {
    case "HEALTHY":
      return {
        label: "HEALTHY",
        banner: "border-success/40 bg-success/10 text-success",
        flash: false,
        description: "Full size bands available per confidence.",
      };
    case "CAUTION":
      return {
        label: "CAUTION",
        banner: "border-warning/40 bg-warning/10 text-warning",
        flash: false,
        description:
          "Reduced size bands — need cumulative above +4 pts for HEALTHY.",
      };
    case "WARNING":
      return {
        label: "WARNING",
        banner: "border-warning/40 bg-warning/10 text-warning",
        flash: false,
        description: "Minimal size only at ≥92% confidence.",
      };
    case "DANGER":
      return {
        label: "DANGER",
        banner: "border-danger/40 bg-danger/10 text-danger",
        flash: false,
        description: "Elevated risk — trading heavily restricted.",
      };
    case "STOP":
      return {
        label: "STOP",
        banner: "border-danger/40 bg-danger/10 text-danger",
        flash: true,
        description: "Trading halted — manual review required.",
      };
    default:
      return {
        label: isNil(stateName) ? "—" : s,
        banner: "border-border bg-card text-muted",
        flash: false,
        description: "Agent state unknown — awaiting data.",
      };
  }
}

function fmtMult(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return null;
  const s = n % 1 === 0 ? n.toFixed(0) : n.toFixed(2).replace(/\.?0+$/, "");
  return `${s}×`;
}

function resolveRiskMeta(state) {
  const gate = (state?.health?.gates || []).find(
    (g) => g.name === "risk_validation",
  );
  const v = gate?.value;
  if (!v || typeof v !== "object") return {};
  return {
    openCount: v.open_count,
    maxPositions: v.max_positions ?? v.max_open ?? v.position_cap,
  };
}

function agentStateDescription(state, stateName) {
  const base = agentStateMeta(stateName).description;
  const s = String(stateName ?? "").toUpperCase();
  const pts = state?.points || {};
  const sig = state?.signal || {};
  const parts = [];

  let mult = pts.size_multiplier ?? state?.size_multiplier;
  if (mult != null && Number.isFinite(Number(mult))) {
    const multN = Number(mult);
    if (s === "CAUTION" && multN === 0) {
      mult = 0.5;
      parts.push(`max ${fmtMult(mult)} size band (<80% conf gate)`);
    } else {
      parts.push(`max ${fmtMult(multN)} size`);
    }
  }

  const configThr = sig.config_signal_threshold;
  const gateThr = sig.threshold ?? pts.trade_threshold;
  const minSize = sig.min_size_threshold;
  if (configThr != null && Number.isFinite(Number(configThr))) {
    parts.push(`config ${Math.round(Number(configThr))}%`);
  }
  if (gateThr != null && Number.isFinite(Number(gateThr))) {
    parts.push(`gate ${Math.round(Number(gateThr))}%`);
  }
  if (minSize != null && Number.isFinite(Number(minSize))) {
    parts.push(`min size ${Math.round(Number(minSize))}%`);
  }

  const { maxPositions, openCount } = resolveRiskMeta(state);
  if (maxPositions != null && Number.isFinite(Number(maxPositions))) {
    const open =
      openCount != null && Number.isFinite(Number(openCount))
        ? ` (${Math.round(Number(openCount))} open)`
        : "";
    parts.push(`max ${Math.round(Number(maxPositions))} positions${open}`);
  }

  if (parts.length === 0) return base;

  const detail = parts.join(", ");
  if (s === "CAUTION") {
    return `${detail} — reduced bands; need cumulative above +4 pts for HEALTHY.`;
  }
  if (s === "WARNING") {
    return `${detail} — minimal size only at ≥92% confidence.`;
  }
  if (s === "HEALTHY") {
    return `${detail} — full size bands per confidence tier.`;
  }
  if (s === "DANGER" || s === "STOP") {
    return `${detail} — ${base}`;
  }
  return `${detail}. ${base}`;
}

function resolveRecentTrades(state) {
  const raw = state?.recent_trades;
  if (!Array.isArray(raw)) return [];
  return raw
    .slice(-20)
    .map((t) => String(t?.result ?? "").toUpperCase())
    .filter((r) => r === "WIN" || r === "LOSS");
}

function resolvePnlHistory(state) {
  const raw = state?.pnl_history;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((row) => {
      const time = row?.time ?? row?.ts ?? row?.timestamp;
      const value = row?.value ?? row?.pnl_gbp ?? row?.pnl;
      const n = Number(value);
      if (!time || !Number.isFinite(n)) return null;
      return { time, value: n };
    })
    .filter(Boolean);
}

function fmtChartTime(ts) {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString("en-GB", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return String(ts);
  }
}

function fmtGbp(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n >= 0 ? "+" : "";
  return `${sign}£${n.toFixed(2)}`;
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

function ScoreCard({ label, value, description, title }) {
  const color = pointsColor(value);
  return (
    <div
      className="flex flex-col rounded-lg border border-border bg-card p-3 sm:p-4"
      title={title}
    >
      <span className="label-caps">{label}</span>
      <span
        className={[
          "mt-1 font-mono text-3xl font-semibold tabular-nums leading-none sm:text-4xl",
          color,
        ].join(" ")}
      >
        {fmtPoints(value)}
      </span>
      <p className="mt-2 text-[11px] leading-snug text-muted">{description}</p>
    </div>
  );
}

function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload;
  const time = row?.time ?? label;
  return (
    <div className="rounded border border-border bg-card px-2 py-1.5 text-[11px] shadow-lg">
      <p className="text-muted">{fmtChartTime(time)}</p>
      <p className="font-medium tabular-nums text-foreground">
        {fmtGbp(payload[0]?.value)}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// PointsPanel
// ---------------------------------------------------------------------------

export default function PointsPanel({ state }) {
  if (!state) {
    return (
      <div className="mx-auto max-w-5xl space-y-3 px-1">
        <div className="rounded-lg border border-border bg-card p-6 text-center text-muted">
          Waiting for state…
        </div>
      </div>
    );
  }

  const pts = state.points;
  const agentState = resolveAgentState(state);
  const agent = agentStateMeta(agentState);
  const agentDesc = agentStateDescription(state, agentState);

  const lastTrade = pts?.last_trade;
  const session = pts?.session;
  const cumulative = pts?.cumulative;

  const recentResults = resolveRecentTrades(state);
  const wins = recentResults.filter((r) => r === "WIN").length;
  const shown = recentResults.length;
  const winPct =
    shown > 0 ? Math.round((wins / shown) * 100) : null;

  const pnlHistory = resolvePnlHistory(state);
  const latestPnl =
    pnlHistory.length > 0 ? pnlHistory[pnlHistory.length - 1].value : null;
  const lineColor =
    latestPnl != null && latestPnl < 0 ? "#ef4444" : "#22c55e";

  const nextTier = pts?.next_tier;
  const nextPts = nextTier?.points_to_next;
  const nextLabel = nextTier?.label;

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">
      {/* 1. Score cards */}
      <div
        className="grid grid-cols-1 gap-2 sm:grid-cols-3 sm:gap-3"
        title="Points are the strategy score (stop-distance units), not £ P&L. The chart below tracks IG-confirmed £ profit."
      >
        <ScoreCard
          label="Trade"
          value={pts ? lastTrade : null}
          description="Points from the last closed trade"
          title="Last trade points — strategy score, not £"
        />
        <ScoreCard
          label="Session"
          value={pts ? session : null}
          description="Session cumulative points score"
          title="Points earned this session"
        />
        <ScoreCard
          label="Cumulative"
          value={pts ? cumulative : null}
          description="Rolling cumulative points (tier driver)"
          title="Rolling points total — drives HEALTHY / CAUTION tiers"
        />
      </div>
      <p className="text-center text-[10px] text-muted">
        Points = strategy score (not £). Cumulative P&amp;L chart below uses IG-confirmed £ only.
      </p>

      {nextLabel ? (
        <Card title="Next tier">
          <p className="text-center text-[13px] text-foreground">{nextLabel}</p>
          {nextPts != null && Number.isFinite(Number(nextPts)) && Number(nextPts) > 0 ? (
            <p className="mt-1 text-center font-mono text-lg tabular-nums text-success">
              +{Number(nextPts).toFixed(1)} pts to unlock
            </p>
          ) : nextTier?.kind === "max" ? (
            <p className="mt-1 text-center text-[12px] text-muted">
              Largest configured size bands active
            </p>
          ) : nextTier?.kind === "stop" ? (
            <p className="mt-1 text-center text-[12px] text-danger">
              Trading halted until STOP is cleared
            </p>
          ) : null}
          <p className="mt-2 text-center text-[10px] text-muted">
            Protection milestones (BE +0.5, trail +0.5, limit ext +0.25) also add points while trades run.
          </p>
        </Card>
      ) : null}

      {/* 2. Agent state */}
      <Card className="text-center">
        <div className="flex flex-col items-center py-2">
          <span
            className={[
              "inline-flex items-center rounded-full border px-4 py-2 text-sm font-semibold uppercase tracking-wide sm:text-base",
              agent.banner,
              agent.flash ? "animate-pulse" : "",
            ].join(" ")}
          >
            {agent.label}
          </span>
          <p className="mt-3 max-w-xl text-[12px] leading-snug text-muted sm:text-[13px]">
            {agentDesc}
          </p>
        </div>
      </Card>

      {/* 3. Win rate sparkline */}
      <Card title="Win rate (last 20)">
        {shown === 0 ? (
          <p className="py-4 text-center text-[12px] text-muted">
            No recent trades to display
          </p>
        ) : (
          <>
            <div className="flex flex-wrap justify-center gap-1">
              {recentResults.map((result, idx) => (
                <span
                  key={idx}
                  title={result}
                  className={[
                    "h-3 w-3 shrink-0 rounded-sm sm:h-3.5 sm:w-3.5",
                    result === "WIN" ? "bg-success" : "bg-danger",
                  ].join(" ")}
                  aria-label={result}
                />
              ))}
            </div>
            <p className="mt-3 text-center text-[12px] tabular-nums text-foreground">
              <span className="font-medium">
                {wins} / {shown}
              </span>
              {winPct != null && (
                <span className="text-muted"> · {winPct}% win rate</span>
              )}
            </p>
          </>
        )}
      </Card>

      {/* 4. Cumulative P&L curve */}
      <Card title="Cumulative P&amp;L">
        {pnlHistory.length === 0 ? (
          <p className="flex h-[200px] items-center justify-center text-[12px] text-muted">
            No P&amp;L history yet
          </p>
        ) : (
          <div className="h-[200px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={pnlHistory}
                margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
              >
                <XAxis
                  dataKey="time"
                  tickFormatter={fmtChartTime}
                  tick={{ fill: "#94a3b8", fontSize: 10 }}
                  axisLine={{ stroke: "#2a3344" }}
                  tickLine={{ stroke: "#2a3344" }}
                  minTickGap={32}
                />
                <YAxis
                  tickFormatter={(v) => `£${Number(v).toFixed(0)}`}
                  tick={{ fill: "#94a3b8", fontSize: 10 }}
                  axisLine={{ stroke: "#2a3344" }}
                  tickLine={{ stroke: "#2a3344" }}
                  width={48}
                />
                <Tooltip content={<ChartTooltip />} />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke={lineColor}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, fill: lineColor }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    </div>
  );
}
