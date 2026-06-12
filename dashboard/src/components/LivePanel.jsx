import { memo, useEffect, useRef, useState } from "react";
import { api } from "../api/client.js";
import { fmtPrice } from "../utils/fmtPrice.js";
import { fmtPts } from "../utils/fmtPts.js";
import MarketStatusTimer, {
  buildMarketStatusTimerProps,
  marketStatusTimerPropsEqual,
} from "./MarketStatusTimer.jsx";
import {
  activeEpicRank,
  isEpicRotationMuted,
  medalForRank,
  resolveActiveEpics,
  resolveGateRelaxations,
} from "../utils/roadmapTelemetry.js";

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
  const raw = String(ts);
  if (/^\d{1,2}:\d{2}:\d{2}$/.test(raw)) return raw;
  try {
    const d = new Date(raw);
    if (Number.isNaN(d.getTime())) return raw;
    return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return raw; }
}

function fmtLogLine(entry) {
  if (entry == null) return "—";
  if (typeof entry === "string") return entry;
  // ML blend decision entries
  if (entry.ml_prob != null) {
    const dir = entry.direction ?? "?";
    const mkt = entry.market ? `[${entry.market}]` : "";
    const prob = `ML ${(entry.ml_prob * 100).toFixed(1)}%`;
    const rules = `rules ${entry.rules_conf?.toFixed(1) ?? "?"}%`;
    const mode = entry.blend_note
      ?? (entry.blended
        ? `→ blended ${entry.confidence?.toFixed(1) ?? "?"}%`
        : "(near-50%, rules used)");
    return [mkt, dir, prob, rules, mode].filter(Boolean).join(" · ");
  }
  const parts = [entry.decision, entry.action, entry.label, entry.setup, entry.direction].filter(Boolean);
  if (parts.length) return parts.join(" · ");
  if (entry.message) return String(entry.message);
  try { return JSON.stringify(entry); } catch { return "—"; }
}

const GATE_ORDER = [
  "session_open",
  "cold_start_gap",
  "environment_fitness",
  "points_state",
  "correlation_ok",
  "risk_validation",
  "expectancy_ok",
  "calendar_ok",
  "signal_confidence",
  "ml_veto",
  "execution",
];

function orderGates(gates) {
  const byName = Object.fromEntries((gates || []).map((g) => [g.name, g]));
  const ordered = GATE_ORDER.map(
    (name) => byName[name] || { name, pass: false, detail: "—", value: null },
  );
  for (const g of gates || []) {
    if (g?.name && !GATE_ORDER.includes(g.name)) ordered.push(g);
  }
  return ordered;
}

function shortenBlockReason(reason) {
  if (!reason) return "";
  const rsi = reason.match(/RSI[^:]*:\s*([\d.]+)\s*>\s*max\s*([\d.]+)/i);
  if (rsi) return `RSI ${rsi[1]} above max ${rsi[2]}`;
  if (reason.length > 72) return `${reason.slice(0, 69)}…`;
  return reason;
}

function resolveRawBlockReason(health, signal, state) {
  if (state?.gate_blocked_reason) return String(state.gate_blocked_reason);
  const ordered = orderGates(health?.gates);
  for (const g of ordered) {
    if (g.pass) continue;
    if (g.name === "signal_confidence" && g.value?.block_reason) {
      return String(g.value.block_reason);
    }
  }
  if (signal?.block_reason) return String(signal.block_reason);
  return null;
}

function getBlockingReason(health, signal, state) {
  const raw = resolveRawBlockReason(health, signal, state);
  if (raw) return shortenBlockReason(raw);
  const ordered = orderGates(health?.gates);
  for (const g of ordered) {
    if (g.pass) continue;
    if (g.detail) return String(g.detail).replace(/^WAIT\s*[—-]\s*/i, "").trim();
  }
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
  return getBlockingReason(state?.health || {}, state?.signal || {}, state);
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

function agentStateMeta(stateName, state) {
  const s = String(stateName ?? "").toUpperCase();
  const relax = resolveGateRelaxations(state);
  const warningCap =
    state?.signal?.threshold ??
    relax?.warning_confidence_cap ??
    92;
  switch (s) {
    case "HEALTHY": return { label: "HEALTHY", banner: "border-success/40 bg-success/10 text-success", description: "Full size bands available per confidence." };
    case "CAUTION":  return { label: "CAUTION",  banner: "border-warning/40 bg-warning/10 text-warning", description: "Reduced size bands — need cumulative above +4 pts for HEALTHY." };
    case "WARNING":  return {
      label: "WARNING",
      banner: "border-warning/40 bg-warning/10 text-warning",
      description: relax?.demo_soak_mode
        ? `Minimal size only at ≥${warningCap}% confidence (demo soak — instrument floor may apply).`
        : `Minimal size only at ≥${warningCap}% confidence.`,
    };
    case "DANGER":   return { label: "DANGER",   banner: "border-danger/40 bg-danger/10 text-danger",    description: "Elevated risk — trading heavily restricted." };
    case "STOP":     return { label: "STOP",     banner: "border-danger/40 bg-danger/10 text-danger animate-pulse", description: "Trading halted — manual review required." };
    default:         return { label: s || "—",   banner: "border-border bg-card text-muted",             description: "Agent state unknown — awaiting data." };
  }
}

/** Whole-number 0–100; never throws on null/undefined/stale ticks. */
function safePct(value, fallback = 0) {
  if (value == null || value === "" || Number.isNaN(Number(value))) return fallback;
  return Math.min(100, Math.max(0, Math.round(Number(value))));
}

function resolveRawSignalConfidence(state) {
  const gateConf = state?.health?.gates?.find((g) => g.name === "signal_confidence")?.value?.confidence;
  if (gateConf != null && !Number.isNaN(Number(gateConf))) return safePct(gateConf, 0);
  const signal = state?.signal || {};
  const rules = signal?.rules_confidence;
  if (rules != null && !Number.isNaN(Number(rules))) return safePct(rules, 0);
  const core = signal?.signal_core_score;
  if (core != null && !Number.isNaN(Number(core))) return safePct(core, 0);
  const fromSig = signal?.confidence ?? state?.signal_strength;
  if (fromSig != null && !Number.isNaN(Number(fromSig))) return safePct(fromSig, 0);
  return null;
}

function resolveMarketRawConfidence(mslice) {
  const gateConf = (mslice?.health?.gates || []).find((g) => g.name === "signal_confidence")?.value?.confidence;
  if (gateConf != null && !Number.isNaN(Number(gateConf))) return safePct(gateConf, 0);
  return safePct(mslice?.signal?.confidence ?? mslice?.signal_strength, 0);
}

function resolveRawDirection(signal) {
  const raw = signal?.raw_direction;
  if (raw && String(raw).toUpperCase() !== "WAIT") return String(raw).toUpperCase();
  return resolveSignalDirection(signal);
}

function resolveGateProgress(health) {
  const gates = orderGates(health?.gates);
  const total = gates.length;
  const passing = gates.filter((g) => g.pass).length;
  const pct = total > 0 ? Math.round((passing / total) * 100) : 0;
  return { passing, total, pct };
}

function resolveSignalDirection(signal) {
  const raw = signal?.direction;
  if (raw == null || raw === "") return "WAIT";
  return String(raw).toUpperCase();
}

function resolveSignalCoreScore(signal, state) {
  const direct = signal?.signal_core_score;
  if (direct != null && !Number.isNaN(Number(direct))) {
    return safePct(direct, 0);
  }
  const rules = signal?.rules_confidence;
  if (rules != null && !Number.isNaN(Number(rules))) {
    return safePct(rules, 0);
  }
  const gateConf = state?.health?.gates?.find((g) => g.name === "signal_confidence")?.value?.confidence;
  if (gateConf != null && !Number.isNaN(Number(gateConf))) {
    return safePct(gateConf, 0);
  }
  return safePct(signal?.signal_core_score, 0);
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

const ML_BLEND_MIN_ROWS = 500;

function resolveMlGauge(state) {
  const value = resolveMlProbability(state);
  if (value != null) {
    return { value, disabled: false, disabledLabel: "ML disabled" };
  }
  if (state?.ml_enabled !== true) {
    return { value: null, disabled: true, disabledLabel: "ML disabled" };
  }
  const rows = Number(state?.ml_training_records ?? 0);
  if (rows < ML_BLEND_MIN_ROWS) {
    return { value: null, disabled: true, disabledLabel: `${rows}/${ML_BLEND_MIN_ROWS} rows` };
  }
  return { value: null, disabled: true, disabledLabel: "Not trained" };
}

function resolvePositions(state) {
  if (Array.isArray(state?.positions) && state.positions.length > 0) return state.positions;
  if (Array.isArray(state?.active_trades) && state.active_trades.length > 0) return state.active_trades;
  // Aggregate from per-market slices (filtered to selected_epic when present)
  const markets = state?.markets;
  if (markets && typeof markets === "object") {
    const epicFilter = state?.selected_epic;
    const all = [];
    for (const [epic, mslice] of Object.entries(markets)) {
      if (epicFilter && epic !== epicFilter) continue;
      const positions = mslice?.positions;
      if (Array.isArray(positions)) {
        positions.forEach((p) => {
          all.push({ epic, market: p.market ?? mslice?.market_name ?? mslice?.market ?? epic, ...p });
        });
      }
    }
    if (all.length > 0) return all;
  }
  return [];
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

function priceHeroPropsEqual(prev, next) {
  return prev.label === next.label && prev.value === next.value && prev.epic === next.epic;
}

const MemoPriceHero = memo(PriceHero, priceHeroPropsEqual);

function LivePriceBlock({ bid, offer, epic, ts, wsConnected }) {
  return (
    <div className="rounded-lg border border-border bg-card p-3 py-4 sm:p-4">
      <div className="flex items-stretch justify-center gap-2 sm:gap-6">
        <MemoPriceHero label="Bid" value={bid} epic={epic} />
        <div className="hidden w-px self-stretch bg-border sm:block" aria-hidden />
        <MemoPriceHero label="Offer" value={offer} epic={epic} />
      </div>
      <p className="mt-3 text-center text-[11px] text-muted">
        Last update <span className="tabular-nums text-foreground">{fmtTs(ts)}</span>
        {!wsConnected && <span className="ml-2 text-warning">· polling</span>}
      </p>
    </div>
  );
}

function livePriceBlockPropsEqual(prev, next) {
  return (
    prev.bid === next.bid
    && prev.offer === next.offer
    && prev.epic === next.epic
    && prev.ts === next.ts
    && prev.wsConnected === next.wsConnected
  );
}

const MemoLivePriceBlock = memo(LivePriceBlock, livePriceBlockPropsEqual);

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

function EntryStatusCard({ signal = {}, state, health = {}, isReady = false, gateBlockedAt = null }) {
  const direction = resolveSignalDirection(signal);
  const rawDirection = resolveRawDirection(signal);
  const confidence = resolveRawSignalConfidence(state);
  const blocked = direction === "WAIT" && rawDirection !== "WAIT";
  const gateProgress = resolveGateProgress(health);
  const rawBlockReason = isReady ? null : resolveRawBlockReason(health, signal, state);
  const blocker = isReady ? null : (rawBlockReason || getBlockingReason(health, signal, state));
  const displayDir = blocked ? rawDirection : (direction !== "WAIT" ? direction : rawDirection);
  const isBuy = displayDir === "BUY";
  const isSell = displayDir === "SELL";
  const dirColor = isBuy ? "text-success" : isSell ? "text-danger" : "text-muted";
  const coreScore = resolveSignalCoreScore(signal, state);

  return (
    <div className="flex flex-col rounded-lg border border-border bg-card p-4 lg:col-span-2">
      <div className="mb-4">
        <p className="label-caps mb-1.5">Signal</p>
        <div className="flex flex-wrap items-center gap-2">
          {displayDir && displayDir !== "WAIT" ? (
            <span className={`font-mono text-2xl font-semibold tabular-nums ${dirColor}`}>
              {displayDir} {confidence != null ? `${confidence}%` : "—"}
            </span>
          ) : (
            <span className="font-mono text-2xl font-semibold text-muted">WAIT</span>
          )}
          {blocked && (
            <span className="inline-flex rounded-full border border-warning/50 bg-warning/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-warning">
              blocked
            </span>
          )}
          {isReady && displayDir && displayDir !== "WAIT" && (
            <span className="inline-flex rounded-full border border-success/50 bg-success/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-success">
              armed
            </span>
          )}
        </div>
        {coreScore != null && (
          <p className="mt-1.5 text-[10px] text-muted">
            Core score <span className="font-mono font-semibold tabular-nums text-blue-400/90">{coreScore}%</span>
          </p>
        )}
      </div>

      <div className="mb-4">
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <p className="label-caps">Gates</p>
          <span className="text-[12px] font-medium tabular-nums text-foreground">
            {gateProgress.passing} of {gateProgress.total} passing
          </span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-border">
          <div
            className={[
              "h-full rounded-full transition-all duration-500",
              isReady ? "bg-success" : gateProgress.pct >= 80 ? "bg-warning" : "bg-accent",
            ].join(" ")}
            style={{ width: `${gateProgress.pct}%` }}
          />
        </div>
      </div>

      {isReady ? (
        <p className="rounded-md border border-success/30 bg-success/5 px-2.5 py-2 text-[12px] font-medium text-success leading-snug">
          Armed — executing this tick
        </p>
      ) : blocker ? (
        <div
          className={[
            "rounded-lg border px-3 py-3",
            rawBlockReason
              ? "border-danger/50 bg-danger/10"
              : "border-warning/50 bg-warning/10",
          ].join(" ")}
        >
          <p className={["label-caps mb-1.5", rawBlockReason ? "text-danger" : "text-warning"].join(" ")}>
            Blocker
          </p>
          {rawBlockReason ? (
            <p className="text-sm font-bold leading-snug text-danger sm:text-base">
              {rawBlockReason}
            </p>
          ) : (
            <p className="text-sm font-semibold leading-snug text-warning">
              {blocker}
            </p>
          )}
          {gateBlockedAt && (
            <p className="mt-1.5 text-[10px] text-muted">Blocked since {fmtTs(gateBlockedAt)}</p>
          )}
        </div>
      ) : (
        <p className="text-[12px] text-muted">No active blocker — awaiting signal</p>
      )}
    </div>
  );
}

function entryStatusPropsEqual(prev, next) {
  if (prev.isReady !== next.isReady) return false;
  if (prev.gateBlockedAt !== next.gateBlockedAt) return false;
  const ps = prev.signal || {};
  const ns = next.signal || {};
  if (
    ps.direction !== ns.direction
    || ps.confidence !== ns.confidence
    || ps.raw_direction !== ns.raw_direction
    || ps.adjusted_score !== ns.adjusted_score
    || ps.raw_score !== ns.raw_score
  ) {
    return false;
  }
  const ph = prev.health || {};
  const nh = next.health || {};
  if (ph.badge !== nh.badge) return false;
  const pg = ph.gates || [];
  const ng = nh.gates || [];
  if (pg.length !== ng.length) return false;
  for (let i = 0; i < pg.length; i += 1) {
    if (pg[i].pass !== ng[i].pass || pg[i].name !== ng[i].name) return false;
  }
  return true;
}

const MemoEntryStatusCard = memo(EntryStatusCard, entryStatusPropsEqual);

function SignalThresholds({ signal = {}, state, pointsState }) {
  const sigGate = (state?.health?.gates || []).find((g) => g.name === "signal_confidence");
  const gate = sigGate?.value;
  const direction = resolveSignalDirection(signal);
  const pick = (sigKey, topKey, gateKey) => {
    const fromSig = signal?.[sigKey];
    if (fromSig != null && !Number.isNaN(Number(fromSig))) return Number(fromSig);
    const fromTop = state?.[topKey];
    if (fromTop != null && !Number.isNaN(Number(fromTop))) return Number(fromTop);
    const fromGate = gate?.[gateKey ?? sigKey];
    if (fromGate != null && !Number.isNaN(Number(fromGate))) return Number(fromGate);
    return null;
  };
  const signalConf = resolveRawSignalConfidence(state);
  const config = pick("config_signal_threshold", "config_signal_threshold", "config_signal_threshold");
  const effective = pick("threshold", "signal_threshold", "threshold");
  const minSize = pick("min_size_threshold", "min_size_threshold", "min_size_threshold");
  const stateLabel = pointsState || signal?.points_state || state?.points?.state || gate?.points_state || "—";

  const rows = [
    { label: "Signal confidence", value: signalConf, highlight: true },
    { label: "Config threshold", value: config, highlight: false },
    { label: `Gate (${stateLabel})`, value: effective, highlight: false },
    { label: "Min size threshold", value: minSize, highlight: false },
  ];

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <p className="label-caps mb-2">Thresholds</p>
      <ul className="space-y-1.5 text-[12px]">
        {rows.map(({ label, value, highlight }) => {
          const n = value != null && !Number.isNaN(Number(value)) ? Number(value) : null;
          const belowMin = highlight && n != null && minSize != null && n < Number(minSize);
          const belowGate = highlight && n != null && effective != null && n < Number(effective) && !belowMin;
          return (
            <li
              key={label}
              className={[
                "flex justify-between gap-3 tabular-nums",
                highlight ? "font-semibold text-foreground" : "text-muted",
                belowMin ? "text-danger" : belowGate ? "text-warning" : "",
              ].join(" ")}
            >
              <span>{label}</span>
              <span>{n != null ? `${Math.round(n)}%` : "—"}</span>
            </li>
          );
        })}
      </ul>
      {direction !== "WAIT" && signalConf != null && minSize != null && Number(signalConf) < Number(minSize) && (
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

const LONDON_TZ = "Europe/London";

function fmtNextOpen(isoStr) {
  if (!isoStr) return null;
  try {
    const open = new Date(isoStr);
    const now = new Date();
    const dayKey = (d) =>
      d.toLocaleDateString("en-GB", {
        timeZone: LONDON_TZ,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      });
    const time = open.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: LONDON_TZ,
    });
    if (dayKey(open) === dayKey(now)) {
      return time;
    }
    const day = open.toLocaleDateString("en-GB", {
      weekday: "short",
      timeZone: LONDON_TZ,
    });
    return `${day} ${time}`;
  } catch {
    return null;
  }
}

const MARKET_SHORT_LABELS = {
  "IX.D.DOW.IFM.IP": "WALL ST",
  "IX.D.NASDAQ.IFM.IP": "NASDAQ",
  "CS.D.CFPGOLD.CFP.IP": "GOLD",
  "IX.D.NIKKEI.IFM.IP": "NIKKEI",
  "IX.D.DAX.IFM.IP": "DAX 40",
  "CS.D.EURUSD.CFD.IP": "EUR/USD",
  "CS.D.GBPUSD.CFD.IP": "GBP/USD",
};

function marketPillLabel(epic, name) {
  if (MARKET_SHORT_LABELS[epic]) return MARKET_SHORT_LABELS[epic];
  const n = String(name ?? "").trim();
  if (!n) return epic;
  if (n.length <= 12) return n.toUpperCase();
  return n.split(/\s+/).slice(0, 2).join(" ").toUpperCase();
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

function MarketCard({
  epic,
  markets,
  labels,
  selectedEpic,
  onSelectEpic,
  activeEpics,
  rawState,
  variant = "compact",
  rotationRank = -1,
}) {
  const m = markets?.[epic] || {};
  const name = m.market || labels[epic] || epic;
  const bid = m.bid;
  const isOpen = String(m.market_state ?? "").toUpperCase() === "OPEN";
  const conf = resolveMarketRawConfidence(m);
  const showConf = isOpen && conf != null;
  const active = epic === selectedEpic;
  const status = marketStatusMeta(m.market_state, m.stream_status);
  const rotationMuted = isEpicRotationMuted(activeEpics, epic, rawState);
  const sessionGateVal = (m.health?.gates || []).find((g) => g.name === "session_open")?.value;
  const nextOpenIso = typeof sessionGateVal === "object" ? sessionGateVal?.next_open : null;
  const nextOpenTime = fmtNextOpen(nextOpenIso);
  const featured = variant === "featured";

  return (
    <button
      type="button"
      onClick={() => onSelectEpic?.(epic)}
      disabled={rotationMuted}
      className={[
        "rounded-lg border transition-all duration-300",
        featured ? "p-3 text-center sm:p-4" : "p-2 text-left",
        rotationMuted
          ? "pointer-events-none border-border/60 bg-surface/50 opacity-40 grayscale"
          : active
            ? "border-accent bg-accent/10 ring-1 ring-accent/30"
            : featured
              ? "border-border bg-card hover:border-accent/50 hover:bg-accent/5"
              : "border-border bg-surface hover:border-accent/40 hover:bg-card",
      ].join(" ")}
    >
      <div
        className={[
          "flex items-center gap-1",
          featured ? "mb-2 justify-center" : "mb-1 justify-between",
        ].join(" ")}
      >
        <span
          className={[
            "font-semibold uppercase tracking-wide",
            featured ? "text-base sm:text-lg" : "truncate text-[10px]",
            active ? "text-accent" : "text-foreground",
          ].join(" ")}
        >
          {rotationRank >= 0 && medalForRank(rotationRank) ? (
            <span className="mr-0.5" aria-hidden>{medalForRank(rotationRank)}</span>
          ) : null}
          {marketPillLabel(epic, name)}
        </span>
        <span className={`shrink-0 rounded-full ${featured ? "h-2 w-2" : "h-1.5 w-1.5"} ${status.dot}`} />
      </div>
      {rotationMuted && (
        <div className={featured ? "mb-2" : "mb-1"}>
          <span className="inline-flex rounded border border-border bg-muted/20 px-1 py-0.5 text-[8px] font-bold uppercase tracking-wide text-muted">
            MUTED — ROTATION
          </span>
        </div>
      )}
      <div
        className={[
          "font-mono tabular-nums",
          featured ? "text-xl font-bold text-foreground sm:text-2xl" : "text-[11px] text-muted",
        ].join(" ")}
      >
        {isOpen && bid != null ? fmtPrice(bid, epic) : "—"}
      </div>
      <div
        className={[
          "flex items-center gap-1",
          featured ? "mt-2 justify-center" : "mt-1 justify-between",
        ].join(" ")}
      >
        <span
          className={[
            "font-bold uppercase tracking-wider",
            featured ? "text-[11px]" : "text-[9px]",
            status.color,
          ].join(" ")}
        >
          {status.label}
        </span>
        {showConf && (
          <span
            className={[
              "tabular-nums font-medium",
              featured ? "text-base" : "text-[10px]",
              signalDot(conf) === "bg-success"
                ? "text-success"
                : signalDot(conf) === "bg-warning"
                  ? "text-warning"
                  : "text-muted",
            ].join(" ")}
          >
            {conf}%
          </span>
        )}
      </div>
      {!isOpen && nextOpenTime && (
        <div className={`text-muted/70 tabular-nums ${featured ? "mt-1 text-[10px]" : "mt-0.5 text-[9px]"}`}>
          Opens {nextOpenTime}
        </div>
      )}
    </button>
  );
}

function marketSliceQuoteKey(markets, epic) {
  const m = markets?.[epic];
  if (!m || typeof m !== "object") return "";
  const sig = m.signal && typeof m.signal === "object" ? m.signal : {};
  return [
    m.bid,
    m.offer,
    m.spread,
    m.market_state,
    m.stream_status,
    m.tick_age_s,
    sig.confidence,
    sig.direction,
  ].join("|");
}

function marketCardPropsEqual(prev, next) {
  if (
    prev.epic !== next.epic
    || prev.selectedEpic !== next.selectedEpic
    || prev.variant !== next.variant
    || prev.rotationRank !== next.rotationRank
  ) {
    return false;
  }
  const prevActive = (prev.activeEpics || []).join(",");
  const nextActive = (next.activeEpics || []).join(",");
  if (prevActive !== nextActive) return false;
  return marketSliceQuoteKey(prev.markets, prev.epic) === marketSliceQuoteKey(next.markets, next.epic);
}

const MemoMarketCard = memo(MarketCard, marketCardPropsEqual);

function marketGridPropsEqual(prev, next) {
  if (prev.selectedEpic !== next.selectedEpic) return false;
  const rawPrev = prev.rawState || {};
  const rawNext = next.rawState || {};
  const activePrev = (resolveActiveEpics(rawPrev) || []).join(",");
  const activeNext = (resolveActiveEpics(rawNext) || []).join(",");
  if (activePrev !== activeNext) return false;
  const enabledPrev = (rawPrev.enabled_epics || Object.keys(rawPrev.markets || {})).join(",");
  const enabledNext = (rawNext.enabled_epics || Object.keys(rawNext.markets || {})).join(",");
  if (enabledPrev !== enabledNext) return false;
  const marketsPrev = rawPrev.markets || {};
  const marketsNext = rawNext.markets || {};
  const epics = enabledPrev.split(",").filter(Boolean);
  for (const epic of epics) {
    if (marketSliceQuoteKey(marketsPrev, epic) !== marketSliceQuoteKey(marketsNext, epic)) {
      return false;
    }
  }
  return true;
}

function MarketGrid({ rawState, selectedEpic, onSelectEpic }) {
  const markets = rawState?.markets;
  const labels = rawState?.instrument_labels || {};
  const enabled = Array.isArray(rawState?.enabled_epics) ? rawState.enabled_epics.filter(Boolean) : [];
  const epics = enabled.length ? enabled : (markets ? Object.keys(markets) : []);
  const activeEpics = resolveActiveEpics(rawState);
  if (epics.length <= 1) return null;

  const featured =
    activeEpics.length > 0
      ? activeEpics.slice(0, 3)
      : epics
          .filter((epic) => String(markets?.[epic]?.market_state ?? "").toUpperCase() === "OPEN")
          .slice(0, 3);
  const featuredSet = new Set(featured);
  const secondary = epics.filter((epic) => !featuredSet.has(epic));

  return (
    <div className="space-y-2">
      {featured.length > 0 && (
        <div className="mx-auto max-w-3xl">
          <p className="label-caps mb-1.5 text-center">Active markets</p>
          <div
            className={[
              "grid gap-2 sm:gap-3",
              featured.length === 1
                ? "grid-cols-1 max-w-xs mx-auto"
                : featured.length === 2
                  ? "grid-cols-2 max-w-lg mx-auto"
                  : "grid-cols-1 sm:grid-cols-3",
            ].join(" ")}
          >
            {featured.map((epic) => (
              <MemoMarketCard
                key={epic}
                epic={epic}
                markets={markets}
                labels={labels}
                selectedEpic={selectedEpic}
                onSelectEpic={onSelectEpic}
                activeEpics={activeEpics}
                rawState={rawState}
                variant="featured"
                rotationRank={activeEpicRank(activeEpics, epic)}
              />
            ))}
          </div>
        </div>
      )}
      {secondary.length > 0 && (
        <div>
          {featured.length > 0 && (
            <p className="label-caps mb-1 text-center text-muted/80">Other markets</p>
          )}
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-6">
            {secondary.map((epic) => (
              <MemoMarketCard
                key={epic}
                epic={epic}
                markets={markets}
                labels={labels}
                selectedEpic={selectedEpic}
                onSelectEpic={onSelectEpic}
                activeEpics={activeEpics}
                rawState={rawState}
                variant="compact"
                rotationRank={activeEpicRank(activeEpics, epic)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

const MemoMarketGrid = memo(MarketGrid, marketGridPropsEqual);

function liveMarketStatusProps(rawState, epic) {
  return buildMarketStatusTimerProps(rawState, epic);
}

function LiveMarketStatusHeader({ rawState, epic }) {
  return <MarketStatusTimer {...liveMarketStatusProps(rawState, epic)} variant="banner" />;
}

const MemoLiveMarketStatusHeader = memo(
  LiveMarketStatusHeader,
  (prev, next) =>
    prev.epic === next.epic
    && marketStatusTimerPropsEqual(
      liveMarketStatusProps(prev.rawState, prev.epic),
      liveMarketStatusProps(next.rawState, next.epic),
    ),
);

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
  const signal    = state?.signal && typeof state.signal === "object" ? state.signal : {};
  const agentState = resolveAgentState(state);
  const agent     = agentStateMeta(agentState, rawState ?? state);
  const positions = resolvePositions(state);
  const gateReason    = resolveGateBlockedReason(state);
  const gateBlockedAt = resolveGateBlockedAt(state);
  const mlGauge   = resolveMlGauge(state);
  const mlProb    = mlGauge.value;
  const mlDisabled = mlGauge.disabled;
  const mlDisabledLabel = mlGauge.disabledLabel;
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

      {(rawState ?? state) && (
        <MemoLiveMarketStatusHeader rawState={rawState ?? state} epic={epic} />
      )}

      {/* 1. Market grid */}
      {(rawState ?? state) && (
        <MemoMarketGrid rawState={rawState ?? state} selectedEpic={selectedEpic} onSelectEpic={onSelectEpic} />
      )}

      {/* 2. Bid/Offer hero */}
      <MemoLivePriceBlock
        bid={state.bid}
        offer={state.offer}
        epic={epic}
        ts={state.ts}
        wsConnected={wsConnected}
      />

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
                  const ptsLabel = ptsNum != null ? `${fmtPts(ptsNum, pos.epic ?? epic)}pts` : "—";
                  return (
                    <tr key={key} className="border-b border-border/60 last:border-0 hover:bg-card/60 transition-colors">
                      <td className="px-2 py-2 text-foreground text-[11px]">{pos.market || pos.epic || "—"}</td>
                      <td className={`px-2 py-2 font-semibold ${sideColor}`}>{side || "—"}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(pos.entry ?? pos.entry_price ?? pos.level, pos.epic ?? epic)}</td>
                      <td className="px-2 py-2 font-mono tabular-nums">{fmtPrice(pos.current ?? pos.mark, pos.epic ?? epic)}</td>
                      <td className={`px-2 py-2 font-mono tabular-nums font-semibold ${pnlColor}`}>
                        <span>{pnlNum != null ? fmtGbp(pnlNum) : ptsLabel}</span>
                        {pnlNum != null && ptsNum != null && (
                          <span className="ml-1 text-[10px] font-normal opacity-70">{ptsLabel}</span>
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

      {/* 5. Entry status + thresholds + ML gauge */}
      <div className="grid grid-cols-1 gap-2 lg:grid-cols-3 sm:gap-3">
        <MemoEntryStatusCard
          signal={signal}
          state={state}
          health={health}
          isReady={allGatesPass && !gateReason}
          gateBlockedAt={gateBlockedAt}
        />
        <SignalThresholds signal={signal} state={state} pointsState={state.points?.state} />
        <Gauge label="ML confidence" value={mlProb} max={1} disabled={mlDisabled} disabledLabel={mlDisabledLabel} formatValue={(v) => v.toFixed(2)} />
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
          <p className="mt-2 text-[10px] text-muted">
            Gate needs ≥{signal.fitness_threshold ?? signal.fitness_factors?.gate_min ?? 55}%.
          </p>
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
