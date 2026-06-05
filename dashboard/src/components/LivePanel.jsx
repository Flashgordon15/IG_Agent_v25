import { useEffect, useRef, useState } from "react";
import { api } from "../api/client.js";
import { fmtPrice } from "../utils/fmtPrice.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtGbp(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n >= 0 ? "+" : "";
  return `${sign}£${n.toFixed(2)}`;
}

function fmtTs(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "medium" });
  } catch { return String(ts); }
}

function fmtLogTs(entry) {
  const ts = entry?.ts ?? entry?.timestamp ?? entry?.time;
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return String(ts); }
}

function fmtLogLine(entry) {
  if (entry == null) return "—";
  if (typeof entry === "string") return entry;
  const parts = [entry.decision, entry.action, entry.label, entry.setup, entry.direction].filter(Boolean);
  if (parts.length) return parts.join(" · ");
  if (entry.message) return String(entry.message);
  try { return JSON.stringify(entry); } catch { return "—"; }
}

const GATE_ORDER = ["session_open","cold_start_gap","environment_fitness","points_state","risk_validation","signal_confidence","execution"];

function orderGates(gates) {
  const byName = Object.fromEntries((gates || []).map((g) => [g.name, g]));
  return GATE_ORDER.map((name) => byName[name] || { name, pass: false, detail: "—", value: null });
}

function shortenBlockReason(reason) {
  if (!reason) return "";
  const rsi = reason.match(/RSI[^:]*:\s*([\d.]+)\s*>\s*max\s*([\d.]+)/i);
  if (rsi) return `RSI ${rsi[1]} above max ${rsi[2]}`;
  if (reason.length > 72) return `${reason.slice(0, 69)}…`;
  return reason;
}

function getBlockingReason(health, signal) {
  const ordered = orderGates(health?.gates);
  for (const g of ordered) {
    if (g.pass) continue;
    if (g.name === "signal_confidence" && g.value?.block_reason) return shortenBlockReason(String(g.value.block_reason));
    if (g.detail) return String(g.detail).replace(/^WAIT\s*[—-]\s*/i, "").trim();
  }
  if (signal?.block_reason) return shortenBlockReason(signal.block_reason);
  const summary = health?.summary || "";
  const dash = summary.indexOf("—");
  if (dash >= 0) {
    const tail = summary.slice(dash + 1).trim();
    if (tail) return tail.replace(/^[^:]+:\s*/, "");
  }
  return null;
}

function firstFailingGate(health) {
  return orderGates(health?.gates).find((g) => !g.pass) ?? null;
}

function resolveGateBlockedReason(state) {
  if (state?.gate_blocked_reason) return state.gate_blocked_reason;
  return getBlockingReason(state?.health || {}, state?.signal || {});
}

function resolveGateBlockedAt(state) {
  if (state?.gate_blocked_at) return state.gate_blocked_at;
  const failing = firstFailingGate(state?.health);
  if (failing?.blocked_at) return failing.blocked_at;
  const reason = resolveGateBlockedReason(state);
  if (reason && state?.ts) return state.ts;
  return null;
}

function resolveAgentState(state) {
  return state?.agent_state ?? state?.points?.state ?? "—";
}

function agentStateMeta(stateName) {
  const s = String(stateName ?? "").toUpperCase();
  switch (s) {
    case "HEALTHY": return { label: "HEALTHY", banner: "border-success/40 bg-success/10 text-success", description: "Full size bands available per confidence." };
    case "CAUTION":  return { label: "CAUTION",  banner: "border-warning/40 bg-warning/10 text-warning", description: "Reduced size bands — need cumulative above +10 pts for HEALTHY." };
    case "WARNING":  return { label: "WARNING",  banner: "border-warning/40 bg-warning/10 text-warning", description: "Minimal size only at ≥92% confidence." };
    case "DANGER":   return { label: "DANGER",   banner: "border-danger/40 bg-danger/10 text-danger",    description: "Elevated risk — trading heavily restricted." };
    case "STOP":     return { label: "STOP",     banner: "border-danger/40 bg-danger/10 text-danger animate-pulse", description: "Trading halted — manual review required." };
    default:         return { label: s || "—",   banner: "border-border bg-card text-muted",             description: "Agent state unknown — awaiting data." };
  }
}

function resolveSignalConfidence(state) {
  const raw = state?.signal?.confidence ?? state?.signal_strength ??
    state?.health?.gates?.find((g) => g.name === "signal_confidence")?.value?.confidence;
  if (raw == null || Number.isNaN(Number(raw))) return null;
  return Math.min(100, Math.max(0, Math.round(Number(raw))));
}

function resolveMlProbability(state) {
  const sigGate = (state?.health?.gates || []).find((g) => g.name === "signal_confidence");
  const fromGate = sigGate?.value?.ml_probability;
  if (fromGate != null && !Number.isNaN(Number(fromGate))) return Number(fromGate);
  const fromSignal = state?.signal?.ml_probability;
  if (fromSignal != null && !Number.isNaN(Number(fromSignal))) return Number(fromSignal);
  const apiMl = state?.ml_confidence;
  if (apiMl != null && !Number.isNaN(Number(apiMl)) && Number(apiMl) <= 1) return Number(apiMl);
  return null;
}

function resolvePositions(state) {
  return state?.positions ?? state?.active_trades ?? [];
}

function resolveMlDecisionLog(state) {
  const log = state?.ml_decision_log;
  return Array.isArray(log) ? log.slice(-50).reverse() : [];
}

// ---------------------------------------------------------------------------
// usePriceFlash
// ---------------------------------------------------------------------------

function usePriceFlash(value) {
  const prev = useRef(value);
  const [flash, setFlash] = useState(null);
  useEffect(() => {
    if (value == null || Number.isNaN(Number(value))) { prev.current = value; return undefined; }
    const num = Number(value);
    const prevNum = prev.current != null ? Number(prev.current) : null;
    if (prevNum != null && !Number.isNaN(prevNum) && num !== prevNum) {
      setFlash(num > prevNum ? "up" : "down");
      const timer = window.setTimeout(() => setFlash(null), 450);
      prev.current = num;
      return () => window.clearTimeout(timer);
    }
    prev.current = num;
    return undefined;
  }, [value]);
  return flash;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function PriceHero({ label, value, epic }) {
  const flash = usePriceFlash(value);
  const flashClass = flash === "up" ? "bg-success/20 ring-2 ring-success/40" : flash === "down" ? "bg-danger/20 ring-2 ring-danger/40" : "ring-0 ring-transparent";
  return (
    <div className="flex flex-1 flex-col items-center">
      <span className="label-caps">{label}</span>
      <span className={["mt-1 rounded-md px-3 py-1 font-mono text-3xl font-semibold tabular-nums leading-none transition-all duration-300 sm:text-4xl text-foreground", flashClass].join(" ")}>
        {fmtPrice(value, epic)}
      </span>
    </div>
  );
}

function Card({ title, children, className = "", titleRight = null }) {
  return (
    <section className={["rounded-lg border border-border bg-card p-3 sm:p-4", className].join(" ")}>
      {(title || titleRight) && (
        <div className="mb-2 flex items-center justify-between gap-2">
          {title && <h2 className="label-caps">{title}</h2>}
          {titleRight}
        </div>
      )}
      {children}
    </section>
  );
}

function GateRow({ gate }) {
  const { name, pass, detail } = gate;
  return (
    <li className="flex items-start gap-2 text-[11px]">
      <span className={`mt-0.5 h-2 w-2 shrink-0 rounded-full ${pass ? "bg-success" : "bg-danger"}`} />
      <span className={`font-medium ${pass ? "text-muted" : "text-foreground"}`}>
        {name.replace(/_/g, " ")}
      </span>
      {!pass && detail && detail !== "—" && (
        <span className="min-w-0 truncate text-muted ml-auto max-w-[180px]" title={String(detail)}>
          {String(detail).length > 40 ? String(detail).slice(0, 37) + "…" : String(detail)}
        </span>
      )}
    </li>
  );
}

function SignalConfidenceBreakdown({ signal, state, pointsState }) {
  const sigGate = (state?.health?.gates || []).find((g) => g.name === "signal_confidence");
  const gate = sigGate?.value;
  const pick = (sigKey, topKey, gateKey) => {
    const fromSig = signal?.[sigKey];
    if (fromSig != null && !Number.isNaN(Number(fromSig))) return Number(fromSig);
    const fromTop = state?.[topKey];
    if (fromTop != null && !Number.isNaN(Number(fromTop))) return Number(fromTop);
    const fromGate = gate?.[gateKey ?? sigKey];
    if (fromGate != null && !Number.isNaN(Number(fromGate))) return Number(fromGate);
    return null;
  };
  const current = pick("confidence", "signal_strength", "confidence");
  const config  = pick("config_signal_threshold", "config_signal_threshold", "config_signal_threshold");
  const effective = pick("threshold", "signal_threshold", "threshold");
  const minSize = pick("min_size_threshold", "min_size_threshold", "min_size_threshold");
  const stateLabel = pointsState || signal?.points_state || state?.points?.state || gate?.points_state || "—";

  const rows = [
    { label: "Config threshold", value: config, highlight: false },
    { label: `Gate (${stateLabel})`, value: effective, highlight: false },
    { label: "Min size threshold", value: minSize, highlight: false },
    { label: "Current", value: current, highlight: true },
  ];

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <p className="label-caps mb-2">Signal confidence</p>
      <ul className="space-y-1.5 text-[12px]">
        {rows.map(({ label, value, highlight }) => {
          const n = value != null && !Number.isNaN(Number(value)) ? Number(value) : null;
          const belowMin = highlight && n != null && minSize != null && n < Number(minSize);
          const belowGate = highlight && n != null && effective != null && n < Number(effective) && !belowMin;
          return (
            <li key={label} className={["flex justify-between gap-3 tabular-nums", highlight ? "font-semibold text-foreground" : "text-muted", belowMin ? "text-danger" : belowGate ? "text-warning" : ""].join(" ")}>
              <span>{label}</span>
              <span>{n != null ? `${Math.round(n)}%` : "—"}</span>
            </li>
          );
        })}
      </ul>
      {current != null && minSize != null && Number(current) < Number(minSize) && (
        <p className="mt-2 text-[11px] text-danger leading-snug">
          Need ≥{Math.round(Number(minSize))}% for 0.5× size in {stateLabel}.
        </p>
      )}
    </div>
  );
}

function Gauge({ label, value, max, disabled, disabledLabel, formatValue }) {
  const pct = disabled || value == null ? 0 : Math.min(100, Math.max(0, (Number(value) / max) * 100));
  const r = 36; const c = 2 * Math.PI * r;
  const offset = c - (pct / 100) * c;
  let strokeClass = "stroke-accent";
  if (!disabled && value != null) {
    const ratio = Number(value) / max;
    if (ratio >= 0.7) strokeClass = "stroke-success";
    else if (ratio >= 0.4) strokeClass = "stroke-warning";
    else strokeClass = "stroke-danger";
  }
  return (
    <div className="flex flex-1 flex-col items-center rounded-lg border border-border bg-card p-3">
      <p className="label-caps">{label}</p>
      <div className="relative my-2 h-[88px] w-[88px]">
        <svg viewBox="0 0 88 88" className="h-full w-full -rotate-90">
          <circle cx="44" cy="44" r={r} fill="none" className="stroke-border" strokeWidth="8" />
          {!disabled && (
            <circle cx="44" cy="44" r={r} fill="none" className={`${strokeClass} transition-all duration-500`}
              strokeWidth="8" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={offset} />
          )}
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          {disabled ? (
            <span className="text-center text-[10px] font-semibold uppercase leading-tight text-muted">{disabledLabel}</span>
          ) : (
            <span className="font-mono text-lg font-semibold tabular-nums text-foreground">{value != null ? formatValue(value) : "—"}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Market overview grid
// ---------------------------------------------------------------------------

function signalDot(conf) {
  if (conf == null) return "bg-border";
  const n = Number(conf);
  if (n >= 80) return "bg-success";
  if (n >= 60) return "bg-warning";
  return "bg-danger/50";
}

function fmtNextOpen(isoStr) {
  if (!isoStr) return null;
  try {
    return new Date(isoStr).toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/London",
    });
  } catch { return null; }
}

function marketStatusMeta(marketState, streamStatus) {
  const ms = String(marketState ?? "").toUpperCase();
  const ss = String(streamStatus ?? "").toUpperCase();
  if (ms === "MAINTENANCE" || ss === "MAINTENANCE") return { label: "MAINT", color: "text-warning", dot: "bg-warning" };
  if (ms === "CLOSED") return { label: "CLOSED", color: "text-muted", dot: "bg-border" };
  if (ms === "OPEN" && ss === "LIVE") return { label: "OPEN", color: "text-success", dot: "bg-success animate-pulse" };
  if (ms === "OPEN") return { label: "OPEN", color: "text-success/70", dot: "bg-success/50" };
  if (ms === "OFFLINE") return { label: "OFFLINE", color: "text-muted", dot: "bg-border" };
  return { label: "—", color: "text-muted", dot: "bg-border" };
}

function MarketGrid({ rawState, selectedEpic, onSelectEpic }) {
  const markets = rawState?.markets;
  const labels  = rawState?.instrument_labels || {};
  const enabled = Array.isArray(rawState?.enabled_epics) ? rawState.enabled_epics.filter(Boolean) : [];
  const epics   = enabled.length ? enabled : (markets ? Object.keys(markets) : []);
  if (epics.length <= 1) return null;

  return (
    <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-6">
      {epics.map((epic) => {
        const m = markets?.[epic] || {};
        const name = m.market || labels[epic] || epic;
        const bid  = m.bid;
        const conf = m.signal?.confidence ?? m.signal_strength;
        const active = epic === selectedEpic;
        const status = marketStatusMeta(m.market_state, m.stream_status);
        const isOpen = String(m.market_state ?? "").toUpperCase() === "OPEN";
        const sessionGateVal = (m.health?.gates || []).find((g) => g.name === "session_open")?.value;
        const nextOpenIso = typeof sessionGateVal === "object" ? sessionGateVal?.next_open : null;
        const nextOpenTime = fmtNextOpen(nextOpenIso);

        return (
          <button
            key={epic}
            type="button"
            onClick={() => onSelectEpic?.(epic)}
            className={[
              "rounded-lg border p-2 text-left transition-all",
              active
                ? "border-accent bg-accent/10"
                : "border-border bg-surface hover:border-accent/40 hover:bg-card",
            ].join(" ")}
          >
            {/* Name + stream dot */}
            <div className="flex items-center justify-between gap-1 mb-1">
              <span className={`text-[10px] font-semibold uppercase tracking-wide truncate ${active ? "text-accent" : "text-foreground"}`}>
                {name.length > 10 ? name.slice(0, 10) : name}
              </span>
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${status.dot}`} />
            </div>
            {/* Price */}
            <div className="font-mono text-[11px] tabular-nums text-muted">
              {isOpen && bid != null ? fmtPrice(bid, epic) : "—"}
            </div>
            {/* Status badge + next open */}
            <div className="mt-1 flex items-center justify-between gap-1">
              <span className={`text-[9px] font-bold uppercase tracking-wider ${status.color}`}>
                {status.label}
              </span>
              {isOpen && conf != null && (
                <span className={`text-[10px] tabular-nums ${signalDot(conf) === "bg-success" ? "text-success" : signalDot(conf) === "bg-warning" ? "text-warning" : "text-muted"}`}>
                  {Math.round(Number(conf))}%
                </span>
              )}
            </div>
            {!isOpen && nextOpenTime && (
              <div className="mt-0.5 text-[9px] text-muted/70 tabular-nums">
                Opens {nextOpenTime}
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Position capacity bar
// ---------------------------------------------------------------------------

function CapacityBar({ positions, maxPositions }) {
  const total = positions.length;
  const max   = Math.max(1, maxPositions ?? 10);
  const pct   = Math.min(100, (total / max) * 100);
  const barColor = total >= max ? "bg-danger" : total >= max * 0.8 ? "bg-warning" : "bg-accent";

  return (
    <div className="flex items-center gap-3">
      <span className="label-caps shrink-0">Capacity</span>
      <div className="flex-1 h-1.5 rounded-full bg-border overflow-hidden">
        <div className={`h-full rounded-full transition-all duration-500 ${barColor}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-[11px] tabular-nums font-semibold shrink-0 ${total >= max ? "text-danger" : total >= max * 0.8 ? "text-warning" : "text-foreground"}`}>
        {total} / {max}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// FlattenAllButton
// ---------------------------------------------------------------------------

function FlattenAllButton() {
  const [step, setStep] = useState(0);
  const [result, setResult] = useState("");
  if (step === 2) return <p className="mt-2 text-[11px] text-muted">Closing all…</p>;
  if (result) return <p className="mt-2 text-[12px] text-muted">{result}</p>;
  if (step === 1) {
    return (
      <div className="mt-3 flex gap-2">
        <button className="flex-1 rounded-md bg-danger py-2 text-[12px] font-semibold text-white"
          onClick={async () => {
            setStep(2);
            try {
              const r = await fetch("/api/flatten/all", { method: "POST" });
              const d = await r.json().catch(() => ({}));
              setResult(d.ok ? `Closed ${d.count ?? 0} position(s).` : "Flatten failed");
            } catch { setResult("Network error"); }
            setStep(0);
          }}>
          Confirm — Close All
        </button>
        <button className="rounded-md border border-border px-3 py-2 text-[12px] text-muted" onClick={() => setStep(0)}>
          Cancel
        </button>
      </div>
    );
  }
  return (
    <div className="mt-3 border-t border-border pt-3">
      <button type="button" onClick={() => setStep(1)}
        className="w-full rounded-md border border-danger/60 py-2 text-[12px] font-semibold text-danger hover:bg-danger/10">
        CLOSE ALL POSITIONS
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Market tab selector (compact pill row)
// ---------------------------------------------------------------------------

function marketTabOptions(rawState) {
  const markets = rawState?.markets && typeof rawState.markets === "object" ? rawState.markets : null;
  const labels  = rawState?.instrument_labels && typeof rawState.instrument_labels === "object" ? rawState.instrument_labels : {};
  const enabled = Array.isArray(rawState?.enabled_epics) ? rawState.enabled_epics.filter(Boolean) : [];
  if (enabled.length) return enabled.map((epic) => {
    const m = markets?.[epic];
    return { epic, label: m?.market || labels[epic] || m?.instrument_id || epic };
  });
  if (markets) return Object.entries(markets).map(([epic, m]) => ({ epic, label: m.market || labels[epic] || m.instrument_id || epic }));
  if (rawState?.epic) return [{ epic: rawState.epic, label: rawState.market || labels[rawState.epic] || rawState.epic }];
  return [];
}

// ---------------------------------------------------------------------------
// LivePanel
// ---------------------------------------------------------------------------

export default function LivePanel({ state, rawState, selectedEpic, onSelectEpic, wsConnected }) {
  const epic      = state?.epic ?? state?.selected_epic ?? selectedEpic ?? "";
  const maxPos    = rawState?.max_open_positions ?? 10;
  const health    = state?.health || {};
  const signal    = state?.signal || {};
  const agentState = resolveAgentState(state);
  const agent     = agentStateMeta(agentState);
  const positions = resolvePositions(state);
  const gateReason    = resolveGateBlockedReason(state);
  const gateBlockedAt = resolveGateBlockedAt(state);
  const failingGate   = firstFailingGate(health);
  const mlProb    = resolveMlProbability(state);
  const mlDisabled = mlProb == null && state?.ml_enabled !== true;
  const mlLog     = resolveMlDecisionLog(state);
  const allGatesPass = orderGates(health.gates).every((g) => g.pass);
  const riskGate  = orderGates(health.gates).find((g) => g.name === "risk_validation");
  const maxPerEpic = Number(riskGate?.value?.max_per_epic ?? 2);
  const epicOpenCount = Number(riskGate?.value?.open_count ?? positions.length);

  const closablePosition = positions.find((p) => !selectedEpic || p.epic === selectedEpic) ?? positions[0];
  const [closeStep, setCloseStep] = useState(0);
  const [closing, setClosing] = useState(false);
  useEffect(() => { setCloseStep(0); }, [closablePosition?.deal_id]);

  const handleClose = async () => {
    if (!closablePosition?.deal_id) return;
    if (closeStep < 1) { setCloseStep(1); return; }
    setClosing(true);
    try {
      await api.closeDeal(closablePosition.deal_id);
      setCloseStep(0);
    } catch (e) { alert(e.message); }
    finally { setClosing(false); }
  };

  if (!state) {
    return (
      <div className="mx-auto max-w-5xl space-y-3 px-1">
        <div className="rounded-lg border border-border bg-card p-8 text-center text-muted">
          Waiting for agent data…
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">

      {/* 1. Market grid */}
      {(rawState ?? state) && (
        <MarketGrid rawState={rawState ?? state} selectedEpic={selectedEpic} onSelectEpic={onSelectEpic} />
      )}

      {/* 2. Bid/Offer hero */}
      <Card className="py-4">
        <div className="flex items-stretch justify-center gap-2 sm:gap-6">
          <PriceHero label="Bid" value={state.bid} epic={epic} />
          <div className="hidden w-px self-stretch bg-border sm:block" aria-hidden />
          <PriceHero label="Offer" value={state.offer} epic={epic} />
        </div>
        <p className="mt-3 text-center text-[11px] text-muted">
          Last update <span className="tabular-nums text-foreground">{fmtTs(state.ts)}</span>
          {!wsConnected && <span className="ml-2 text-warning">· polling</span>}
        </p>
      </Card>

      {/* 3. Agent state banner */}
      <div className={["w-full rounded-lg border px-3 py-2.5 sm:px-4", agent.banner].join(" ")}>
        <p className="text-sm font-bold uppercase tracking-wide">{agent.label}</p>
        <p className="mt-0.5 text-[12px] leading-snug opacity-90">{agent.description}</p>
      </div>

      {/* 4. Active trades table */}
      <Card title="Active trades" titleRight={
        <div className="min-w-[180px]">
          <CapacityBar positions={positions} maxPositions={maxPos} />
        </div>
      }>
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[520px] text-left text-[11px] sm:text-[12px]">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="px-2 py-1.5 font-normal">Market</th>
                <th className="px-2 py-1.5 font-normal">Side</th>
                <th className="px-2 py-1.5 font-normal">Entry</th>
                <th className="px-2 py-1.5 font-normal">Current</th>
                <th className="px-2 py-1.5 font-normal">P&amp;L</th>
                <th className="px-2 py-1.5 font-normal">Stop</th>
                <th className="px-2 py-1.5 font-normal">Open</th>
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-2 py-4 text-center text-muted">No open positions</td>
                </tr>
              ) : (
                positions.map((pos, idx) => {
                  const pnl = pos.pnl_gbp ?? pos.unrealised_pnl_gbp ?? pos.upl;
                  const pnlNum = pnl != null ? Number(pnl) : null;
                  const ptsNum = pos.pnl_pts != null ? Number(pos.pnl_pts) : null;
                  const pnlColor = pnlNum != null ? (pnlNum >= 0 ? "text-success" : "text-danger")
                                  : ptsNum != null ? (ptsNum >= 0 ? "text-success" : "text-danger")
                                  : "text-foreground";
                  const side = String(pos.side ?? pos.direction ?? "").toUpperCase();
                  const sideColor = side === "BUY" ? "text-success" : side === "SELL" ? "text-danger" : "text-foreground";
                  const stop = pos.stop ?? pos.stop_level;
                  const trailLabel = stop != null ? `${fmtPrice(stop, pos.epic ?? epic)}${pos.trail_active ? " ↕" : ""}` : "—";
                  const key = pos.deal_id ?? pos.id ?? `${pos.epic ?? "row"}-${idx}`;
                  const ptsSign = ptsNum != null ? (ptsNum >= 0 ? "+" : "") : "";
                  return (
                    <tr key={key} className="border-b border-border/60 last:border-0 hover:bg-card/60 transition-colors">
                      <td className="px-2 py-2 text-foreground text-[11px]">{pos.market || pos.epic || "—"}</td>
                      <td className={`px-2 py-2 font-semibold ${sideColor}`}>{side || "—"}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(pos.entry ?? pos.entry_price ?? pos.level, pos.epic ?? epic)}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(pos.current ?? pos.mark, pos.epic ?? epic)}</td>
                      <td className={`px-2 py-2 font-mono tabular-nums font-semibold ${pnlColor}`}>
                        <span>{pnlNum != null ? fmtGbp(pnlNum) : (ptsNum != null ? `${ptsSign}${ptsNum.toFixed(1)}pts` : "—")}</span>
                        {ptsNum != null && (
                          <span className="ml-1 text-[10px] font-normal opacity-70">{`${ptsSign}${ptsNum.toFixed(1)}pts`}</span>
                        )}
                      </td>
                      <td className="px-2 py-2 font-mono tabular-nums text-muted">{trailLabel}</td>
                      <td className="px-2 py-2 tabular-nums text-muted">{pos.open_mins != null ? `${Math.round(Number(pos.open_mins))}m` : "—"}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        {closablePosition?.deal_id && (
          <div className="mt-3 border-t border-border pt-3">
            <button type="button" disabled={closing} onClick={handleClose}
              className={["w-full rounded-md py-2 text-[12px] font-medium transition-colors", closeStep === 1 ? "bg-danger text-white" : "border border-danger text-danger hover:bg-danger/10"].join(" ")}>
              {closeStep === 0 ? `Close ${closablePosition.side ?? "position"} — ${closablePosition.deal_id}` : closing ? "Closing…" : "Confirm close — click again"}
            </button>
            {closeStep === 1 && (
              <button type="button" className="mt-2 w-full text-[11px] text-muted" onClick={() => setCloseStep(0)}>Cancel</button>
            )}
          </div>
        )}
        {positions.length > 1 && <FlattenAllButton />}
      </Card>

      {/* 5. Gate status */}
      <Card title="Entry gates">
        {allGatesPass && !gateReason ? (
          <p className="text-[12px] text-success font-medium">All gates passing — ready when signal fires.</p>
        ) : (
          <div className="space-y-2">
            <ul className="grid grid-cols-1 gap-1 sm:grid-cols-2">
              {orderGates(health.gates).map((g) => <GateRow key={g.name} gate={g} />)}
            </ul>
            {gateReason && (
              <p className="mt-1 rounded-md border border-warning/30 bg-warning/5 px-2.5 py-1.5 text-[11px] text-warning leading-snug">
                {gateReason}
              </p>
            )}
            {gateBlockedAt && (
              <p className="text-[10px] text-muted">Blocked since {fmtTs(gateBlockedAt)}</p>
            )}
          </div>
        )}
      </Card>

      {/* 6. Signal confidence + ML gauge */}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 sm:gap-3">
        <SignalConfidenceBreakdown signal={signal} state={state} pointsState={state.points?.state} />
        <Gauge label="ML confidence" value={mlProb} max={1} disabled={mlDisabled} disabledLabel="ML disabled" formatValue={(v) => v.toFixed(2)} />
      </div>

      {/* 7. Environment fitness */}
      {signal.fitness_factors && typeof signal.fitness_factors === "object" && (
        <Card title={`Environment fitness — ${signal.fitness ?? "—"}%`}>
          <ul className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
            {[["ATR", signal.fitness_factors.atr, signal.fitness_factors.max?.atr],
              ["Trend", signal.fitness_factors.trend, signal.fitness_factors.max?.trend],
              ["Session", signal.fitness_factors.session, signal.fitness_factors.max?.session],
              ["Spread", signal.fitness_factors.spread, signal.fitness_factors.max?.spread],
            ].map(([name, pts, max]) => (
              <li key={name} className="flex justify-between gap-2 text-muted">
                <span>{name}</span>
                <span className="font-mono text-foreground tabular-nums">
                  {pts != null ? Math.round(Number(pts)) : "—"} / {max != null ? Math.round(Number(max)) : "—"}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[10px] text-muted">Gate needs ≥{signal.fitness_threshold ?? 40}%.</p>
        </Card>
      )}

      {/* 8. ML decision log */}
      <Card title="ML decision log">
        <div className="max-h-48 overflow-y-auto rounded border border-border/60 bg-bg/50">
          {mlLog.length === 0 ? (
            <p className="px-2 py-3 text-center text-[11px] text-muted">No ML decisions recorded</p>
          ) : (
            <ul className="divide-y divide-border/60">
              {mlLog.map((entry, idx) => (
                <li key={entry.id ?? idx} className="flex flex-wrap items-baseline gap-x-2 px-2 py-1.5 text-[11px]">
                  <span className="shrink-0 tabular-nums text-muted">{fmtLogTs(entry)}</span>
                  <span className="min-w-0 flex-1 text-foreground">{fmtLogLine(entry)}</span>
                  {entry.confidence != null && (
                    <span className="shrink-0 tabular-nums text-accent">
                      {Number(entry.confidence) <= 1 ? Number(entry.confidence).toFixed(2) : `${Math.round(Number(entry.confidence))}%`}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
        <p className="mt-1.5 text-[10px] text-muted">Latest {Math.min(50, mlLog.length)} entries</p>
      </Card>
    </div>
  );
}
