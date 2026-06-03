import { useEffect, useRef, useState } from "react";
import { api } from "../api/client.js";
import { fmtPrice } from "../utils/fmtPrice.js";

function FlattenAllButton() {
  const [step, setStep] = useState(0); // 0=idle, 1=confirm, 2=loading
  const [result, setResult] = useState("");
  if (step === 2) return <p className="mt-2 text-[11px] text-muted">Closing all…</p>;
  if (result) return <p className="mt-2 text-[12px] text-muted">{result}</p>;
  if (step === 1) {
    return (
      <div className="mt-3 flex gap-2">
        <button
          className="flex-1 rounded bg-danger py-2 text-[12px] font-semibold text-white"
          onClick={async () => {
            setStep(2);
            try {
              const r = await fetch("/api/flatten/all", { method: "POST" });
              const d = await r.json().catch(() => ({}));
              setResult(d.ok ? `Closed ${d.count ?? 0} position(s).` : "Flatten failed");
            } catch { setResult("Network error"); }
            setStep(0);
          }}
        >
          Confirm — Close All
        </button>
        <button
          className="rounded border border-border px-3 py-2 text-[12px] text-muted"
          onClick={() => setStep(0)}
        >
          Cancel
        </button>
      </div>
    );
  }
  return (
    <div className="mt-3 border-t border-border pt-3">
      <button
        type="button"
        onClick={() => setStep(1)}
        className="w-full rounded border border-danger/60 py-2 text-[12px] font-semibold text-danger hover:bg-danger/10"
      >
        CLOSE ALL POSITIONS
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers — gate / block reason (aligned with LiveTab)
// ---------------------------------------------------------------------------

const GATE_ORDER = [
  "session_open",
  "cold_start_gap",
  "environment_fitness",
  "points_state",
  "risk_validation",
  "signal_confidence",
  "execution",
];

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
  if (reason.length > 72) return `${reason.slice(0, 69)}…`;
  return reason;
}

function getBlockingReason(health, signal) {
  const ordered = orderGates(health?.gates);
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
  const health = state?.health || {};
  const signal = state?.signal || {};
  return getBlockingReason(health, signal);
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
    case "HEALTHY":
      return {
        label: "HEALTHY",
        banner: "border-success/40 bg-success/10 text-success",
        description: "Full size bands available per confidence.",
      };
    case "CAUTION":
      return {
        label: "CAUTION",
        banner: "border-warning/40 bg-warning/10 text-warning",
        description: "Reduced size bands — need cumulative above +10 pts for HEALTHY.",
      };
    case "WARNING":
      return {
        label: "WARNING",
        banner: "border-warning/40 bg-warning/10 text-warning",
        description: "Minimal size only at ≥92% confidence.",
      };
    case "DANGER":
      return {
        label: "DANGER",
        banner: "border-danger/40 bg-danger/10 text-danger",
        description: "Elevated risk — trading heavily restricted.",
      };
    case "STOP":
      return {
        label: "STOP",
        banner: "border-danger/40 bg-danger/10 text-danger animate-pulse",
        description: "Trading halted — manual review required.",
      };
    default:
      return {
        label: s || "—",
        banner: "border-border bg-card text-muted",
        description: "Agent state unknown — awaiting data.",
      };
  }
}

function resolveSignalConfidence(state) {
  const raw =
    state?.signal?.confidence ??
    state?.signal_strength ??
    state?.health?.gates?.find((g) => g.name === "signal_confidence")?.value
      ?.confidence;
  if (raw == null || Number.isNaN(Number(raw))) return null;
  return Math.min(99, Math.max(0, Math.round(Number(raw))));
}

function resolveMlProbability(state) {
  const sigGate = (state?.health?.gates || []).find(
    (g) => g.name === "signal_confidence",
  );
  const fromGate = sigGate?.value?.ml_probability;
  if (fromGate != null && !Number.isNaN(Number(fromGate))) return Number(fromGate);

  const fromSignal = state?.signal?.ml_probability;
  if (fromSignal != null && !Number.isNaN(Number(fromSignal))) return Number(fromSignal);

  const apiMl = state?.ml_confidence;
  if (apiMl != null && !Number.isNaN(Number(apiMl)) && Number(apiMl) <= 1) {
    return Number(apiMl);
  }

  return null;
}

function resolvePositions(state) {
  return state?.positions ?? state?.active_trades ?? [];
}

function resolveMlDecisionLog(state) {
  const log = state?.ml_decision_log;
  return Array.isArray(log) ? log.slice(-50).reverse() : [];
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

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function usePriceFlash(value) {
  const prev = useRef(value);
  const [flash, setFlash] = useState(null);

  useEffect(() => {
    if (value == null || Number.isNaN(Number(value))) {
      prev.current = value;
      return undefined;
    }
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

function PriceHero({ label, value, epic }) {
  const flash = usePriceFlash(value);
  const flashClass =
    flash === "up"
      ? "bg-success/20 ring-2 ring-success/40"
      : flash === "down"
        ? "bg-danger/20 ring-2 ring-danger/40"
        : "ring-0 ring-transparent";

  return (
    <div className="flex flex-1 flex-col items-center">
      <span className="label-caps">{label}</span>
      <span
        className={[
          "mt-1 rounded-md px-3 py-1 font-mono text-3xl font-semibold tabular-nums leading-none transition-all duration-300 sm:text-4xl",
          "text-foreground",
          flashClass,
        ].join(" ")}
      >
        {fmtPrice(value, epic)}
      </span>
    </div>
  );
}

function resolveThresholdFields(signal, state, pointsState) {
  const sigGate = (state?.health?.gates || []).find(
    (g) => g.name === "signal_confidence",
  );
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
  return {
    current: pick("confidence", "signal_strength", "confidence"),
    config: pick("config_signal_threshold", "config_signal_threshold", "config_signal_threshold"),
    effective: pick("threshold", "signal_threshold", "threshold"),
    minSize: pick("min_size_threshold", "min_size_threshold", "min_size_threshold"),
    stateLabel:
      pointsState ||
      signal?.points_state ||
      state?.points?.state ||
      gate?.points_state ||
      "—",
  };
}

function SignalConfidenceBreakdown({ signal, state, pointsState }) {
  const { current, config, effective, minSize, stateLabel } = resolveThresholdFields(
    signal,
    state,
    pointsState,
  );

  const rows = [
    { label: "Config threshold", value: config, highlight: false },
    {
      label: `Effective gate (${stateLabel})`,
      value: effective,
      highlight: false,
    },
    { label: "Min size threshold", value: minSize, highlight: false },
    { label: "Current", value: current, highlight: true },
  ];

  return (
    <div className="rounded-lg border border-border bg-card p-3 sm:p-4">
      <p className="label-caps mb-2">Signal confidence</p>
      <ul className="space-y-1.5 text-[12px]">
        {rows.map(({ label, value, highlight }) => {
          const n = value != null && !Number.isNaN(Number(value)) ? Number(value) : null;
          const belowMin =
            highlight && n != null && minSize != null && n < Number(minSize);
          const belowGate =
            highlight &&
            n != null &&
            effective != null &&
            n < Number(effective) &&
            !belowMin;
          return (
            <li
              key={label}
              className={[
                "flex justify-between gap-3 tabular-nums",
                highlight ? "font-semibold text-foreground" : "text-muted",
                belowMin ? "text-danger" : belowGate ? "text-warning" : "",
              ].join(" ")}
            >
              <span className={highlight ? "" : ""}>{label}</span>
              <span>{n != null ? `${Math.round(n)}%` : "—"}</span>
            </li>
          );
        })}
      </ul>
      {current != null &&
        minSize != null &&
        Number(current) < Number(minSize) && (
          <p className="mt-2 text-[11px] leading-snug text-danger">
            Need ≥{Math.round(Number(minSize))}% for 0.5× size in {stateLabel} (config{" "}
            {config != null ? Math.round(Number(config)) : "—"}% alone is not enough).
          </p>
        )}
    </div>
  );
}

function Gauge({ label, value, max, disabled, disabledLabel, formatValue }) {
  const pct =
    disabled || value == null
      ? 0
      : Math.min(100, Math.max(0, (Number(value) / max) * 100));
  const r = 36;
  const c = 2 * Math.PI * r;
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
          <circle
            cx="44"
            cy="44"
            r={r}
            fill="none"
            className="stroke-border"
            strokeWidth="8"
          />
          {!disabled && (
            <circle
              cx="44"
              cy="44"
              r={r}
              fill="none"
              className={`${strokeClass} transition-all duration-500`}
              strokeWidth="8"
              strokeLinecap="round"
              strokeDasharray={c}
              strokeDashoffset={offset}
            />
          )}
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          {disabled ? (
            <span className="text-center text-[10px] font-semibold uppercase leading-tight text-muted">
              {disabledLabel}
            </span>
          ) : (
            <span className="font-mono text-lg font-semibold tabular-nums text-foreground">
              {formatValue(value)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

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

// ---------------------------------------------------------------------------
// LivePanel
// ---------------------------------------------------------------------------

function marketTabOptions(rawState) {
  const markets =
    rawState?.markets && typeof rawState.markets === "object"
      ? rawState.markets
      : null;
  const labels =
    rawState?.instrument_labels && typeof rawState.instrument_labels === "object"
      ? rawState.instrument_labels
      : {};

  const enabled = Array.isArray(rawState?.enabled_epics)
    ? rawState.enabled_epics.filter(Boolean)
    : [];

  if (enabled.length) {
    return enabled.map((epic) => {
      const m = markets?.[epic];
      return {
        epic,
        label: m?.market || labels[epic] || m?.instrument_id || epic,
      };
    });
  }

  if (markets) {
    return Object.entries(markets).map(([epic, m]) => ({
      epic,
      label: m.market || labels[epic] || m.instrument_id || epic,
    }));
  }

  if (rawState?.epic) {
    return [
      {
        epic: rawState.epic,
        label: rawState.market || labels[rawState.epic] || rawState.epic,
      },
    ];
  }
  return [];
}

export default function LivePanel({
  state,
  rawState,
  selectedEpic,
  onSelectEpic,
  wsConnected,
}) {
  const epic = state?.epic ?? state?.selected_epic ?? selectedEpic ?? "";
  const tabs = marketTabOptions(rawState ?? state);

  if (!state) {
    return (
      <div className="mx-auto max-w-5xl space-y-3 px-1">
        <div className="rounded-lg border border-border bg-card p-6 text-center text-muted">
          Waiting for state…
        </div>
      </div>
    );
  }

  const health = state.health || {};
  const signal = state.signal || {};
  const agentState = resolveAgentState(state);
  const agent = agentStateMeta(agentState);
  const positions = resolvePositions(state);
  const gateReason = resolveGateBlockedReason(state);
  const gateBlockedAt = resolveGateBlockedAt(state);
  const failingGate = firstFailingGate(health);
  const mlProb = resolveMlProbability(state);
  const mlDisabled = mlProb == null && state?.ml_enabled !== true;
  const mlLog = resolveMlDecisionLog(state);
  const allGatesPass = orderGates(health.gates).every((g) => g.pass);
  const inTrade =
    positions.length > 0 &&
    signal?.direction &&
    signal.direction !== "WAIT";
  const riskGate = orderGates(health.gates).find((g) => g.name === "risk_validation");
  const maxPerEpic = Number(riskGate?.value?.max_per_epic ?? 3);
  const epicOpenCount = Number(riskGate?.value?.open_count ?? positions.length);
  const canStackMore = epicOpenCount < maxPerEpic;

  const closablePosition =
    positions.find((p) => !selectedEpic || p.epic === selectedEpic) ?? positions[0];
  const [closeStep, setCloseStep] = useState(0);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    setCloseStep(0);
  }, [closablePosition?.deal_id]);

  const handleClose = async () => {
    if (!closablePosition?.deal_id) return;
    if (closeStep < 1) {
      setCloseStep(1);
      return;
    }
    setClosing(true);
    try {
      await api.closeDeal(closablePosition.deal_id);
      setCloseStep(0);
    } catch (e) {
      alert(e.message);
    } finally {
      setClosing(false);
    }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">
      {tabs.length > 1 && (
        <div className="flex flex-wrap gap-1 rounded-lg border border-border bg-card p-1">
          {tabs.map((tab) => {
            const active = tab.epic === selectedEpic;
            return (
              <button
                key={tab.epic}
                type="button"
                onClick={() => onSelectEpic?.(tab.epic)}
                className={[
                  "rounded-md px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide transition-colors sm:text-xs",
                  active
                    ? "bg-accent text-accent-foreground"
                    : "text-muted hover:bg-muted/20 hover:text-foreground",
                ].join(" ")}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      )}

      {/* 1. Bid/Offer hero */}
      <Card className="py-4">
        <div className="flex items-stretch justify-center gap-2 sm:gap-6">
          <PriceHero label="Bid" value={state.bid} epic={epic} />
          <div className="hidden w-px self-stretch bg-border sm:block" aria-hidden />
          <PriceHero label="Offer" value={state.offer} epic={epic} />
        </div>

        {/* 2. Last update time */}
        <p className="mt-3 text-center text-[11px] text-muted">
          Last update{" "}
          <span className="tabular-nums text-foreground">{fmtTs(state.ts)}</span>
          {!wsConnected && (
            <span className="ml-2 text-warning">· polling</span>
          )}
        </p>
      </Card>

      {/* 3. Agent state banner */}
      <div
        className={[
          "w-full rounded-lg border px-3 py-2.5 sm:px-4",
          agent.banner,
        ].join(" ")}
      >
        <p className="text-sm font-semibold uppercase tracking-wide">
          {agent.label}
        </p>
        <p className="mt-0.5 text-[12px] leading-snug opacity-90">
          {agent.description}
        </p>
      </div>

      {/* 4. Active trades table */}
      <Card title="Active trades">
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[520px] text-left text-[11px] sm:text-[12px]">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="px-2 py-1.5 font-normal">Epic</th>
                <th className="px-2 py-1.5 font-normal">Side</th>
                <th className="px-2 py-1.5 font-normal">Entry</th>
                <th className="px-2 py-1.5 font-normal">Current</th>
                <th className="px-2 py-1.5 font-normal">P&amp;L GBP</th>
                <th className="px-2 py-1.5 font-normal">Trail Stop</th>
                <th className="px-2 py-1.5 font-normal">Open (mins)</th>
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-2 py-4 text-center text-muted"
                  >
                    No open positions
                  </td>
                </tr>
              ) : (
                positions.map((pos, idx) => {
                  const pnl = pos.pnl_gbp ?? pos.unrealised_pnl_gbp ?? pos.upl;
                  const pnlNum = pnl != null ? Number(pnl) : null;
                  const pnlColor =
                    pnlNum == null
                      ? "text-foreground"
                      : pnlNum >= 0
                        ? "text-success"
                        : "text-danger";
                  const side = String(pos.side ?? pos.direction ?? "").toUpperCase();
                  const sideColor =
                    side === "BUY" ? "text-success" : side === "SELL" ? "text-danger" : "text-foreground";
                  const stop = pos.stop ?? pos.stop_level;
                  const trailLabel =
                    stop != null
                      ? `${fmtPrice(stop, pos.epic ?? epic)}${pos.trail_active ? " T" : ""}`
                      : "—";
                  const key =
                    pos.deal_id ??
                    pos.id ??
                    `${pos.epic ?? "row"}-${idx}`;

                  return (
                    <tr
                      key={key}
                      className="border-b border-border/60 last:border-0"
                    >
                      <td className="px-2 py-2 font-mono text-foreground">
                        {pos.epic || "—"}
                      </td>
                      <td className={`px-2 py-2 font-medium ${sideColor}`}>
                        {side || "—"}
                      </td>
                      <td className="px-2 py-2 tabular-nums">
                        {fmtPrice(pos.entry ?? pos.entry_price ?? pos.level, pos.epic ?? epic)}
                      </td>
                      <td className="px-2 py-2 tabular-nums">
                        {fmtPrice(pos.current ?? pos.mark, pos.epic ?? epic)}
                      </td>
                      <td className={`px-2 py-2 tabular-nums font-medium ${pnlColor}`}>
                        {fmtGbp(pnlNum)}
                      </td>
                      <td className="px-2 py-2 tabular-nums">{trailLabel}</td>
                      <td className="px-2 py-2 tabular-nums">
                        {pos.open_mins != null ? Math.round(Number(pos.open_mins)) : "—"}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        {closablePosition?.deal_id && (
          <div className="mt-3 border-t border-border pt-3">
            <button
              type="button"
              disabled={closing}
              onClick={handleClose}
              className={[
                "w-full rounded-md py-2 text-[12px] font-medium transition-colors",
                closeStep === 1
                  ? "bg-danger text-white"
                  : "border border-danger text-danger hover:bg-danger/10",
              ].join(" ")}
            >
              {closeStep === 0
                ? `Close ${closablePosition.side ?? "position"} (${closablePosition.deal_id})`
                : closing
                  ? "Closing…"
                  : "Confirm close — click again"}
            </button>
            {closeStep === 1 && (
              <button
                type="button"
                className="mt-2 w-full text-[11px] text-muted"
                onClick={() => setCloseStep(0)}
              >
                Cancel
              </button>
            )}
          </div>
        )}
        {positions.length > 1 && <FlattenAllButton />}
      </Card>

      {/* 5. Why No Trade gate card */}
      <Card title="Why no trade">
        {allGatesPass && !gateReason ? (
          <p className="text-[12px] text-success">
            All gates passing — agent ready when signal fires.
          </p>
        ) : inTrade && !allGatesPass ? (
          <p className="text-[12px] text-amber">
            In trade — {signal.direction}{" "}
            {signal.confidence != null ? `${Math.round(signal.confidence)}%` : ""}{" "}
            signal still active;{" "}
            {canStackMore
              ? `add-on available (${epicOpenCount}/${maxPerEpic} on epic).`
              : `max ${maxPerEpic} position(s) per epic reached.`}{" "}
            {gateReason ? `Blocked: ${gateReason}` : ""}
          </p>
        ) : (
          <div className="space-y-1.5 text-[12px]">
            {gateBlockedAt && (
              <p className="text-muted">
                Blocked at{" "}
                <span className="tabular-nums text-foreground">
                  {fmtTs(gateBlockedAt)}
                </span>
              </p>
            )}
            {failingGate?.name && (
              <p className="text-muted">
                Gate{" "}
                <span className="text-foreground">
                  {failingGate.name.replace(/_/g, " ")}
                </span>
              </p>
            )}
            <p className="leading-snug text-foreground">
              {gateReason ||
                health.badge_text ||
                health.summary ||
                signal.block_reason ||
                "Session closed or awaiting data"}
            </p>
            {health.readiness?.label && (
              <p className="text-[11px] text-muted">{health.readiness.label}</p>
            )}
          </div>
        )}
      </Card>

      {/* 6. Signal confidence thresholds + ML gauge */}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 sm:gap-3">
        <SignalConfidenceBreakdown
          signal={signal}
          state={state}
          pointsState={state.points?.state}
        />
        <Gauge
          label="ML confidence"
          value={mlProb}
          max={1}
          disabled={mlDisabled}
          disabledLabel="ML disabled"
          formatValue={(v) => v.toFixed(2)}
        />
      </div>

      {signal.fitness_factors && typeof signal.fitness_factors === "object" && (
        <Card title={`Environment fitness ${signal.fitness ?? "—"}%`}>
          <ul className="space-y-1 text-[11px] font-mono">
            {[
              ["ATR", signal.fitness_factors.atr, signal.fitness_factors.max?.atr],
              ["Trend", signal.fitness_factors.trend, signal.fitness_factors.max?.trend],
              ["Session", signal.fitness_factors.session, signal.fitness_factors.max?.session],
              ["Spread", signal.fitness_factors.spread, signal.fitness_factors.max?.spread],
            ].map(([name, pts, max]) => (
              <li key={name} className="flex justify-between gap-2 text-muted">
                <span>{name}</span>
                <span className="text-foreground tabular-nums">
                  {pts != null ? Math.round(Number(pts)) : "—"} /{" "}
                  {max != null ? Math.round(Number(max)) : "—"}
                </span>
              </li>
            ))}
            {signal.fitness_factors.sentiment_adjustment ? (
              <li className="flex justify-between gap-2 text-amber">
                <span>Sentiment adj</span>
                <span className="tabular-nums">
                  {Math.round(Number(signal.fitness_factors.sentiment_adjustment))}
                </span>
              </li>
            ) : null}
          </ul>
          <p className="mt-2 text-[10px] text-muted leading-snug">
            Environment gate needs ≥{signal.fitness_threshold ?? 40}% (sum of ATR / trend /
            session / spread factors).
          </p>
        </Card>
      )}

      {/* 7. ML decision log */}
      <Card title="ML decision log">
        <div className="max-h-48 overflow-y-auto rounded border border-border/60 bg-bg/50">
          {mlLog.length === 0 ? (
            <p className="px-2 py-3 text-center text-[11px] text-muted">
              No ML decisions recorded
            </p>
          ) : (
            <ul className="divide-y divide-border/60">
              {mlLog.map((entry, idx) => (
                <li
                  key={entry.id ?? idx}
                  className="flex flex-wrap items-baseline gap-x-2 px-2 py-1.5 text-[11px]"
                >
                  <span className="shrink-0 tabular-nums text-muted">
                    {fmtLogTs(entry)}
                  </span>
                  <span className="min-w-0 flex-1 text-foreground">
                    {fmtLogLine(entry)}
                  </span>
                  {entry.confidence != null && (
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
        <p className="mt-1.5 text-[10px] text-muted">
          Showing latest {Math.min(50, mlLog.length)} entries
        </p>
      </Card>
    </div>
  );
}
