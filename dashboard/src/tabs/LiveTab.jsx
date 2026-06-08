import { useState } from "react";
import { api } from "../api/client";
import { fmtPrice } from "../utils/fmtPrice.js";

function barWidth(v, max = 100) {
  return `${Math.min(100, Math.max(0, (v / max) * 100))}%`;
}

/** Gate-aligned bar fill: green at/above threshold, amber when close, red below. */
function barColor(value, threshold, { warnRatio = 0.7 } = {}) {
  if (value == null || Number.isNaN(Number(value))) return "bg-border";
  const t = Number(threshold);
  if (!Number.isFinite(t) || t <= 0) return "bg-blue";
  const v = Number(value);
  if (v >= t) return "bg-green";
  if (v >= t * warnRatio) return "bg-amber";
  return "bg-red";
}

const FITNESS_GATE_MIN = 40;

function signalBarThreshold(label, signal) {
  if (label === "Confidence") {
    return signal.min_size_threshold ?? signal.threshold;
  }
  if (label === "Fitness") return signal.fitness_threshold ?? FITNESS_GATE_MIN;
  if (label === "ATR") return signal.atr_threshold;
  return null;
}

/** Whole-number 0–100 for % displays (fitness, confidence). */
function pct2(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return String(Math.min(99, Math.max(0, Math.round(Number(n)))));
}

/** Backend gate evaluation order — gates 1–8 fixed in UI. */
const GATE_ORDER = [
  "session_open",
  "cold_start_gap",
  "environment_fitness",
  "points_state",
  "risk_validation",
  "expectancy_ok",
  "signal_confidence",
  "execution",
];

/** Always render gates 1–8 in GATE_ORDER (no fail-first sort). */
function orderGates(gates) {
  const byName = Object.fromEntries((gates || []).map((g) => [g.name, g]));
  return GATE_ORDER.map(
    (name) =>
      byName[name] || {
        name,
        pass: false,
        detail: "—",
        value: null,
      },
  );
}

function shortenBlockReason(reason) {
  if (!reason) return "";
  const rsi = reason.match(/RSI[^:]*:\s*([\d.]+)\s*>\s*max\s*([\d.]+)/i);
  if (rsi) return `RSI ${rsi[1]} above max ${rsi[2]}`;
  if (reason.length > 56) return `${reason.slice(0, 53)}…`;
  return reason;
}

/** First failing gate detail, else signal / health summary tail. */
function getBlockingReason(health, signal) {
  const ordered = orderGates(health.gates);
  for (const g of ordered) {
    if (g.pass) continue;
    if (g.name === "signal_confidence" && g.value?.block_reason) {
      return shortenBlockReason(String(g.value.block_reason));
    }
    if (g.detail) {
      return String(g.detail)
        .replace(/^WAIT\s*[—-]\s*/i, "")
        .trim();
    }
    const formatted = formatGateValue(g);
    if (formatted && formatted !== "—") return formatted;
  }
  if (signal?.block_reason) return shortenBlockReason(signal.block_reason);
  const summary = health.summary || "";
  const dash = summary.indexOf("—");
  if (dash >= 0) {
    const tail = summary.slice(dash + 1).trim();
    if (tail) return tail.replace(/^[^:]+:\s*/, "");
  }
  return null;
}

function buildSignalStatusLine(signal) {
  const raw = signal.raw_direction;
  const dir =
    raw && raw !== "WAIT" ? raw : signal.direction && signal.direction !== "WAIT" ? signal.direction : null;
  if (signal.block_reason && dir) {
    return `${dir} blocked — ${shortenBlockReason(signal.block_reason)}`;
  }
  if (dir && signal.confidence != null) {
    return `${dir} ${pct2(signal.confidence)}% — watching`;
  }
  return "WAIT — watching";
}

function formatGateValue(gate) {
  const { name, value } = gate;
  if (value == null) return "—";
  if (name === "environment_fitness") {
    if (value && typeof value === "object" && value.display) return String(value.display);
    if (typeof value === "number") return `${pct2(value)}%`;
  }
  if (typeof value !== "object") return String(value);

  if (name === "cold_start_gap") {
    const bars = value.bars ?? "—";
    const cold = value.cold ? "cold" : "ready";
    const gap = value.gap ? "gap" : "ok";
    return `${bars} bars · ${cold} · ${gap}`;
  }

  if (name === "risk_validation") {
    const spread = value.spread;
    const open = value.open_count;
    const risk = value.risk_gbp;
    const parts = [];
    if (spread != null) parts.push(`spread ${Number(spread).toFixed(1)} pts`);
    if (open != null) parts.push(open === 0 ? "flat (0 open)" : `${open} open`);
    if (risk != null) parts.push(`~£${Math.round(risk)} at risk/trade`);
    return parts.length ? parts.join(" · ") : "—";
  }

  if (name === "signal_confidence") {
    const conf = value.confidence;
    const threshold = value.threshold;
    const dir = value.direction ?? value.signal?.signal;
    const raw = value.raw_direction ?? value.signal?.raw_direction;
    if (value.block_reason && conf != null) {
      const head =
        dir === "WAIT" && raw && raw !== "WAIT"
          ? `${raw} ${pct2(conf)}% → WAIT`
          : `${dir ?? "WAIT"} ${pct2(conf)}%`;
      return `${head} · ${value.block_reason}`;
    }
    if (dir != null && conf != null && threshold != null) {
      return `${dir} ${pct2(conf)}% (need ${pct2(threshold)}%)`;
    }
    if (conf != null && threshold != null) {
      return `${pct2(conf)}% (need ${pct2(threshold)}%)`;
    }
  }

  try {
    return JSON.stringify(value);
  } catch {
    return "—";
  }
}

function formatGateValueCompact(gate) {
  const { name, value } = gate;
  if (name === "environment_fitness") {
    if (value && typeof value === "object" && value.display) return String(value.display);
    if (typeof value === "number") return `${pct2(value)}%`;
  }
  if (name === "session_open") {
    if (gate.detail && /maintenance/i.test(String(gate.detail))) {
      return String(gate.detail);
    }
    return value === true ? "open" : "closed";
  }
  if (name === "points_state") return String(value ?? "—");
  if (name === "cold_start_gap" && value && typeof value === "object") {
    return `${value.bars ?? 0} bars`;
  }
  if (name === "risk_validation" && value && typeof value === "object") {
    const spread = value.spread;
    if (spread != null) return `${Number(spread).toFixed(1)} pts`;
  }
  if (name === "signal_confidence" && value && typeof value === "object") {
    const conf = value.confidence;
    const dir = value.raw_direction ?? value.direction;
    if (dir && conf != null) return `${dir} ${pct2(conf)}%`;
  }
  if (name === "execution") return String(value ?? "armed");
  const full = formatGateValue(gate);
  return full.length > 36 ? `${full.slice(0, 33)}…` : full;
}

function GateCard({ gate, wide }) {
  const pass = gate.pass;
  const border = pass ? "border-l-green" : "border-l-red";
  return (
    <div
      className={`card-live border-l-4 ${border} ${wide ? "col-span-2" : ""} min-h-[4.5rem] flex flex-col justify-center`}
    >
      <p className="label-caps">{gate.name?.replace(/_/g, " ")}</p>
      {pass ? (
        <p className="text-muted text-[11px] mt-1 leading-snug">{formatGateValueCompact(gate)}</p>
      ) : (
        <>
          <p className="text-red text-[12px] mt-1 leading-snug">{gate.detail || "Blocked"}</p>
          <p className="text-muted text-[11px] mt-0.5 leading-snug">{formatGateValue(gate)}</p>
        </>
      )}
    </div>
  );
}

export default function LiveTab({ tick }) {
  const [closeStep, setCloseStep] = useState(0);
  const [closing, setClosing] = useState(false);
  const health = tick?.health || {};
  const badgeRaw = String(health.badge || "WATCHING").toUpperCase();
  const badge = ["WATCHING", "BLOCKED", "READY"].includes(badgeRaw) ? badgeRaw : "WATCHING";
  const blockingReason = getBlockingReason(health, tick?.signal || {});
  const marketMaint = tick?.market_state === "MAINTENANCE";
  const sessionGate = (health.gates || []).find((g) => g.name === "session_open");
  const badgeColor =
    badge === "READY" ? "text-green" : badge === "BLOCKED" ? "text-red" : "text-amber";
  const gates = orderGates(health.gates);
  const signal = tick?.signal || {};
  const pos = (tick?.positions || [])[0];
  const signalLine = buildSignalStatusLine(signal);

  const handleClose = async () => {
    if (!pos?.deal_id) return;
    if (closeStep < 1) {
      setCloseStep(1);
      return;
    }
    setClosing(true);
    try {
      await api.closeDeal(pos.deal_id);
      setCloseStep(0);
    } catch (e) {
      alert(e.message);
    } finally {
      setClosing(false);
    }
  };

  const entry = Number(pos?.entry) || 0;
  const current = Number(pos?.current) || entry;
  const stop = Number(pos?.stop) || entry;
  const target = Number(pos?.target) || entry;
  const range = target - stop || 1;
  const progress = ((current - stop) / range) * 100;

  return (
    <div className="live-tab p-4 space-y-1.5 max-w-5xl mx-auto">
      <div className="card-live text-center">
        <p className="label-caps">Master health</p>
        <p className={`price-lg mt-1 ${badgeColor}`}>{badge}</p>
        <p className="text-muted text-[11px] mt-1 leading-snug">
          {badge === "READY"
            ? "All gates passing"
            : marketMaint
              ? sessionGate?.detail ||
                "Japan 225 daily maintenance (~22:00 BST) — prices resume automatically"
              : blockingReason || (badge === "WATCHING" ? "Session closed or awaiting data" : "—")}
        </p>
      </div>

      <div className="grid md:grid-cols-2 gap-1.5">
        <div className="card-live">
          <p className="label-caps mb-2">Price</p>
          <div className="grid grid-cols-4 gap-2 text-center">
            <div>
              <p className="text-muted text-[11px]">Bid</p>
              <p className="price-lg">{fmtPrice(tick?.bid, tick?.epic) ?? "—"}</p>
            </div>
            <div>
              <p className="text-muted text-[11px]">Spread</p>
              <p className="text-amber font-medium">{tick?.spread?.toFixed(1) ?? "—"}</p>
            </div>
            <div>
              <p className="text-muted text-[11px]">Offer</p>
              <p className="price-lg">{fmtPrice(tick?.offer, tick?.epic) ?? "—"}</p>
            </div>
            <div>
              <p className="text-muted text-[11px]">Tick age</p>
              <p className="text-white font-medium">{tick?.tick_age_s ?? "—"}s</p>
            </div>
          </div>
        </div>

        <div className="card-live space-y-2">
          <p className="label-caps">Signal bars</p>
          {["Confidence", "Fitness", "ATR"].map((label) => {
            const val =
              label === "Confidence"
                ? signal.confidence
                : label === "Fitness"
                  ? signal.fitness
                  : signal.atr;
            const threshold = signalBarThreshold(label, signal);
            const barMax =
              label === "ATR" && threshold != null && Number(threshold) > 0
                ? Math.max(Number(threshold) * 1.5, Number(val) || 0, 1)
                : 100;
            const ff = signal.fitness_factors;
            const factorRows =
              label === "Fitness" && ff && typeof ff === "object"
                ? [
                    ["ATR", ff.atr, ff.max?.atr],
                    ["Trend", ff.trend, ff.max?.trend],
                    ["Session", ff.session, ff.max?.session],
                    ["Spread", ff.spread, ff.max?.spread],
                  ]
                : null;
            return (
              <div key={label}>
                <div className="flex justify-between text-[11px] mb-1">
                  <span className="text-muted">{label}</span>
                  <span>
                    {label === "Confidence" || label === "Fitness"
                      ? val != null
                        ? `${pct2(val)}%`
                        : "—"
                      : (val ?? "—")}
                  </span>
                </div>
                <div className="h-2 rounded bg-bg overflow-hidden">
                  <div
                    className={`h-full ${barColor(val, threshold)}`}
                    style={{ width: barWidth(Number(val) || 0, barMax) }}
                  />
                </div>
                {label === "Confidence" && signal.config_signal_threshold != null && (
                  <ul className="mt-1 space-y-0.5 text-[10px] text-muted">
                    <li className="flex justify-between">
                      <span>Config</span>
                      <span className="tabular-nums">
                        {Math.round(Number(signal.config_signal_threshold))}%
                      </span>
                    </li>
                    <li className="flex justify-between">
                      <span>Gate ({signal.points_state ?? "—"})</span>
                      <span className="tabular-nums">
                        {Math.round(Number(signal.threshold ?? 0))}%
                      </span>
                    </li>
                    <li className="flex justify-between font-medium text-foreground">
                      <span>Min size</span>
                      <span className="tabular-nums">
                        {Math.round(Number(signal.min_size_threshold ?? 0))}%
                      </span>
                    </li>
                  </ul>
                )}
                {factorRows && (
                  <ul className="mt-1 space-y-0.5 text-[10px] text-muted font-mono">
                    {factorRows.map(([name, pts, max]) => (
                      <li key={name} className="flex justify-between gap-2">
                        <span>{name}</span>
                        <span>
                          {pts != null ? Math.round(Number(pts)) : "—"}/
                          {max != null ? Math.round(Number(max)) : "—"}
                        </span>
                      </li>
                    ))}
                    {ff.sentiment_adjustment ? (
                      <li className="flex justify-between gap-2 text-amber">
                        <span>Sentiment</span>
                        <span>{Math.round(Number(ff.sentiment_adjustment))}</span>
                      </li>
                    ) : null}
                  </ul>
                )}
              </div>
            );
          })}
          <p className="text-[11px] text-muted pt-1 border-t border-border leading-snug">{signalLine}</p>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-1.5">
        {[
          ["Open P&L", pos?.pnl_gbp != null ? `£${Number(pos.pnl_gbp).toFixed(2)}` : "—"],
          ["Daily P&L", `£${Number(tick?.daily_pnl_gbp || 0).toFixed(2)}`],
          ["Win rate (20)", tick?.win_rate_20 != null ? `${tick.win_rate_20}%` : "—"],
          ["Balance", tick?.balance_gbp != null ? `£${Number(tick.balance_gbp).toFixed(2)}` : "—"],
        ].map(([k, v]) => (
          <div key={k} className="card-live">
            <p className="label-caps">{k}</p>
            <p className="price-lg mt-1">{v}</p>
          </div>
        ))}
      </div>

      <div>
        <p className="label-caps mb-2">Gates</p>
        <div className="grid grid-cols-2 gap-1.5">
          {gates.map((g) => (
            <GateCard key={g.name} gate={g} wide={g.name === "signal_confidence"} />
          ))}
        </div>
      </div>

      {pos && (
        <div className="card-live border border-border">
          <div className="flex justify-between items-start mb-4">
            <span
              className={`px-2 py-0.5 rounded text-[11px] font-medium ${
                pos.side === "BUY" ? "bg-green/20 text-green" : "bg-red/20 text-red"
              }`}
            >
              {pos.side}
            </span>
            <p className="price-lg text-green">
              {pos.pnl_gbp != null ? `£${Number(pos.pnl_gbp).toFixed(2)}` : "—"}
            </p>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
            {[
              ["Entry", pos.entry],
              ["Current", pos.current],
              ["Stop", pos.stop],
              ["Target", pos.target],
              ["Trail", pos.trail_active ? "Active" : "Off"],
              ["Break-even", pos.breakeven_hit ? "Hit" : "No"],
              ["To stop", pos.stop != null && pos.current != null ? (pos.current - pos.stop).toFixed(1) : "—"],
              ["To target", pos.target != null && pos.current != null ? (pos.target - pos.current).toFixed(1) : "—"],
            ].map(([k, v]) => (
              <div key={k}>
                <p className="label-caps">{k}</p>
                <p className="text-white">{v ?? "—"}</p>
              </div>
            ))}
          </div>
          <div className="mt-4 h-2 rounded bg-bg relative overflow-hidden">
            <div className="absolute left-0 top-0 h-full w-1 bg-red" />
            <div
              className="absolute top-0 h-full w-1 bg-green"
              style={{ left: `${Math.min(100, Math.max(0, progress))}%` }}
            />
          </div>
          <div className="border-t border-border mt-4 pt-4">
            <button
              type="button"
              disabled={closing}
              onClick={handleClose}
              className={`w-full py-2 rounded text-[13px] font-medium ${
                closeStep === 1
                  ? "bg-red text-white"
                  : "border border-red text-red hover:bg-red/10"
              }`}
            >
              {closeStep === 0
                ? "Close position now"
                : closing
                  ? "Closing…"
                  : "Confirm close — click again"}
            </button>
            {closeStep === 1 && (
              <button
                type="button"
                className="w-full mt-2 text-muted text-[11px]"
                onClick={() => setCloseStep(0)}
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
