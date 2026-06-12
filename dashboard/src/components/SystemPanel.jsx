import { useRef, useState } from "react";
import { postEmergencyStop } from "../api.js";
import ActiveGatesRibbon from "./ActiveGatesRibbon.jsx";
import LearningHealthPanel from "./LearningHealthPanel.jsx";
import ShadowVsLivePerformanceCard from "./ShadowVsLivePerformanceCard.jsx";
import ManualInterventionControlCard from "./ManualInterventionControlCard.jsx";
import SentinelDiagnosticConsole from "./SentinelDiagnosticConsole.jsx";
import resolveSupervisionAlert from "../utils/supervision.js";
import {
  resolveEffectivePolicy,
  resolveGateRelaxations,
} from "../utils/roadmapTelemetry.js";

async function postJson(url) {
  const r = await fetch(url, { method: "POST" });
  try { return await r.json(); } catch { return { ok: r.ok }; }
}

// ---------------------------------------------------------------------------
// Stable-value hook — remembers the last non-null/non-"—" value so fields
// never flicker back to "—" when the backend momentarily omits a key.
// ---------------------------------------------------------------------------

function useStableValue(val, emptyFallback = "—") {
  const ref = useRef(val);
  const isBlank = val == null || val === "—" || val === "";
  if (!isBlank) ref.current = val;
  return ref.current ?? emptyFallback;
}

// ---------------------------------------------------------------------------
// Helpers
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

function fmtTs(ts) {
  if (!ts || ts === "—" || ts === "Never") return null;
  try {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleString("en-GB", {
      dateStyle: "short",
      timeStyle: "medium",
    });
  } catch {
    return null;
  }
}

function isWatchdogFailed(state) {
  if (!state) return false;
  if (state.watchdog_failed === true) return true;
  if (typeof state.watchdog_failed === "string" && state.watchdog_failed.trim()) {
    return true;
  }
  return false;
}

function resolveRestUsed(state) {
  const raw =
    state?.rest_budget ??
    state?.rest_budget_used ??
    state?.rest_calls_min ??
    0;
  const n = Number(raw);
  return Number.isFinite(n) ? Math.max(0, Math.round(n)) : 0;
}

const REST_BUDGET_CAP = 3;

function restBudgetMeta(used) {
  if (used < REST_BUDGET_CAP) {
    return { bar: "bg-success", text: "text-success" };
  }
  if (used === REST_BUDGET_CAP) {
    return { bar: "bg-warning", text: "text-warning" };
  }
  return { bar: "bg-danger", text: "text-danger" };
}

function resolveStreamStatus(state, wsConnected) {
  const raw = String(state?.stream_status ?? "").toUpperCase();
  if (raw === "LIVE" || raw === "STALE" || raw === "DISCONNECTED") {
    return raw;
  }
  if (!wsConnected) return "DISCONNECTED";
  const age = Number(state?.tick_age_s);
  if (Number.isFinite(age) && age > 30) return "STALE";
  return "DISCONNECTED";
}

function streamStatusMeta(status) {
  switch (status) {
    case "LIVE":
      return { label: "LIVE", className: "text-success" };
    case "STALE":
      return { label: "STALE", className: "text-warning" };
    default:
      return { label: "DISCONNECTED", className: "text-danger" };
  }
}

function resolveWsMeta(wsConnected, reconnecting) {
  if (wsConnected) {
    return { label: "Connected", dot: "bg-success" };
  }
  if (reconnecting) {
    return { label: "Reconnecting", dot: "bg-warning" };
  }
  return { label: "Disconnected", dot: "bg-danger" };
}

function resolvePositionSyncStatus(state) {
  return (
    state?.position_sync_status ??
    state?.position_sync?.sync_status ??
    state?.ig_position_sync_status ??
    "—"
  );
}

function resolveModelVersion(state) {
  return state?.model_version ?? state?.ml?.model_version ?? null;
}

function resolveLastRetrain(state) {
  return state?.last_retrain_time ?? state?.last_retrain ?? null;
}

function resolveUptime(state) {
  return state?.uptime ?? state?.agent_uptime ?? null;
}

function resolveLastError(state) {
  const err = state?.last_error ?? state?.errors?.type ?? null;
  if (err == null || err === "") return null;
  return String(err);
}

function StatusRow({ label, children }) {
  return (
    <div className="flex items-start justify-between gap-3 text-[12px] sm:text-[13px]">
      <dt className="shrink-0 text-muted">{label}</dt>
      <dd className="min-w-0 text-right">{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SystemPanel
// ---------------------------------------------------------------------------

export default function SystemPanel({ state, wsConnected, reconnecting }) {
  // Stable hooks must be at the top level — before any early return.
  const stableUptime = useStableValue(resolveUptime(state));
  const stablePositionSync = useStableValue(resolvePositionSyncStatus(state));
  const stableOhlcCount = useStableValue(
    state?.ohlc_markets_cached != null ? state.ohlc_markets_cached : null,
    null,
  );

  const [modalOpen, setModalOpen] = useState(false);
  const [confirmInput, setConfirmInput] = useState("");
  const [resultMsg, setResultMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [agentModal, setAgentModal] = useState(null); // "stop" | "restart" | null
  const [agentConfirmInput, setAgentConfirmInput] = useState("");
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentResult, setAgentResult] = useState("");
  const [flattenLoading, setFlattenLoading] = useState(false);
  const [flattenResult, setFlattenResult] = useState("");
  const [reconcileLoading, setReconcileLoading] = useState(false);
  const [reconcileResult, setReconcileResult] = useState("");

  const openAgentModal = (type) => {
    setAgentModal(type);
    setAgentConfirmInput("");
    setAgentResult("");
  };
  const closeAgentModal = () => {
    if (agentLoading) return;
    setAgentModal(null);
    setAgentConfirmInput("");
    setAgentResult("");
  };
  const handleAgentAction = async () => {
    if (agentConfirmInput.toUpperCase() !== "CONFIRM" || agentLoading) return;
    setAgentLoading(true);
    setAgentResult("");
    const endpoint = agentModal === "stop" ? "/api/agent/stop" : "/api/agent/restart";
    const r = await postJson(endpoint);
    setAgentLoading(false);
    setAgentResult(
      r.ok
        ? agentModal === "stop" ? "Agent stopped." : "Agent restarted."
        : "Action failed — check engine.log"
    );
  };

  const handleFlattenAll = async () => {
    setFlattenLoading(true);
    setFlattenResult("");
    const r = await postJson("/api/flatten/all");
    setFlattenLoading(false);
    setFlattenResult(r.ok ? `Closed ${r.count ?? 0} position(s).` : "Flatten failed — check logs");
  };

  const openModal = () => {
    setConfirmInput("");
    setResultMsg("");
    setModalOpen(true);
  };

  const closeModal = () => {
    if (loading) return;
    setModalOpen(false);
    setConfirmInput("");
    setResultMsg("");
  };

  const handleConfirm = async () => {
    if (confirmInput !== "STOP" || loading) return;
    setLoading(true);
    setResultMsg("");
    const result = await postEmergencyStop();
    setLoading(false);
    if (result.ok) {
      setResultMsg("Agent locked. Restart required.");
    } else {
      setResultMsg("Request failed — check engine.log");
    }
  };

  if (!state) {
    return (
      <div className="mx-auto max-w-5xl space-y-3 px-1">
        <div className="rounded-lg border border-border bg-card p-6 text-center text-muted">
          Waiting for state…
        </div>
      </div>
    );
  }

  const restUsed = resolveRestUsed(state);
  const restMeta = restBudgetMeta(restUsed);
  const streamStatus = resolveStreamStatus(state, wsConnected);
  const streamMeta = streamStatusMeta(streamStatus);
  const wsMeta = resolveWsMeta(wsConnected, reconnecting);
  const lastError = resolveLastError(state);
  const modelVersion = resolveModelVersion(state);
  const lastRetrain = resolveLastRetrain(state);
  const uptime = stableUptime;
  const supervisionAlert = resolveSupervisionAlert(state);
  const overnight = state?.overnight_supervision || {};
  const gateRelax = resolveGateRelaxations(state);
  const effectivePolicy = resolveEffectivePolicy(state);
  const dailyLoss = effectivePolicy?.daily_loss;

  const handleReconcile = async () => {
    setReconcileLoading(true);
    setReconcileResult("");
    const r = await postJson("/api/trades/reconcile");
    setReconcileLoading(false);
    setReconcileResult(
      r.ok ? "Reconcile scheduled — check RECONCILE_TRUTH in engine.log" : "Reconcile failed",
    );
  };

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">
      <div className="-mx-1 overflow-hidden rounded-lg border border-border">
        <ActiveGatesRibbon state={state} />
      </div>

      <SentinelDiagnosticConsole />

      <ManualInterventionControlCard state={state} />

      {effectivePolicy?.learning_demo_enabled && (
        <div
          className="rounded-lg border border-sky-500/35 bg-sky-500/10 px-3 py-2.5 text-[11px] leading-snug text-sky-100 sm:text-xs"
          role="status"
        >
          <p className="font-semibold uppercase tracking-wide">
            Operating policy — Profile {effectivePolicy.profile ?? "B"} ({effectivePolicy.policy_id ?? "—"})
          </p>
          <p className="mt-1 text-sky-100/90">
            Gate-sourced submit:{" "}
            {effectivePolicy.integrity?.require_gate_sourced_submit ? "required" : "off"}
            {" · "}v26 relaxations:{" "}
            {effectivePolicy.integrity?.suppress_v26_gate_relaxations ? "suppressed" : "active"}
            {" · "}dynamic sizing:{" "}
            {effectivePolicy.integrity?.disable_dynamic_sizing ? "disabled" : "on"}
            {dailyLoss != null && (
              <>
                {" · "}effective loss £{dailyLoss.effective_loss_gbp ?? 0}
                {" / "}soft £{dailyLoss.soft_pause_gbp ?? 400}
                {" / "}hard £{dailyLoss.hard_limit_gbp ?? 500}
              </>
            )}
            {dailyLoss?.reset?.version && (
              <>
                {" · "}reset {dailyLoss.reset.version} @ {dailyLoss.reset.reset_at ?? "—"}
              </>
            )}
          </p>
        </div>
      )}
      {gateRelax?.demo_soak_mode && (
        <div
          className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-[11px] leading-snug text-amber-100 sm:text-xs"
          role="status"
        >
          <p className="font-semibold uppercase tracking-wide">Demo soak mode active</p>
          <p className="mt-1 text-amber-100/90">
            Rotation bypassed · fitness floor {gateRelax.fitness_min ?? 50}% · WARNING cap{" "}
            {gateRelax.warning_confidence_cap ?? 85}%
            {gateRelax.spread_to_atr_circuit_max != null
              ? ` · spread/ATR max ${gateRelax.spread_to_atr_circuit_max}`
              : ""}
            . Set <span className="font-mono">demo_soak_mode.enabled: false</span> in config_v29 before live.
          </p>
        </div>
      )}
      {supervisionAlert && (
        <div
          className={[
            "rounded-lg border px-3 py-2.5 text-[11px] leading-snug sm:text-xs",
            supervisionAlert.severity === "danger"
              ? "border-danger/50 bg-danger/10 text-danger"
              : "border-warning/50 bg-warning/10 text-warning",
          ].join(" ")}
          role="alert"
        >
          <p className="font-semibold">{supervisionAlert.title}</p>
          <ul className="mt-1 list-inside list-disc">
            {(supervisionAlert.messages || []).map((msg) => (
              <li key={msg}>{msg}</li>
            ))}
          </ul>
        </div>
      )}

      {isWatchdogFailed(state) && (
        <div
          className="animate-pulse rounded-lg border border-danger bg-danger px-3 py-2.5 text-center text-[11px] font-semibold leading-snug text-white sm:text-xs"
          role="alert"
        >
          ⚠ WATCHDOG FAILURE — delete src/data/logs/watchdog_failed.txt and
          restart the agent to clear this alert
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Card title="Connectivity">
          <dl className="space-y-3">
            <div>
              <div className="mb-1 flex items-center justify-between gap-2 text-[12px] sm:text-[13px]">
                <span className="text-muted">REST Budget</span>
                <span className={`font-mono tabular-nums ${restMeta.text}`}>
                  {restUsed} / {REST_BUDGET_CAP}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded bg-border">
                <div
                  className={["h-full transition-all duration-300", restMeta.bar].join(
                    " ",
                  )}
                  style={{ width: `${Math.min(100, (restUsed / REST_BUDGET_CAP) * 100)}%` }}
                />
              </div>
            </div>

            <StatusRow label="Stream Status">
              <span className={`font-semibold uppercase ${streamMeta.className}`}>
                {streamMeta.label}
              </span>
            </StatusRow>

            <StatusRow label="WebSocket">
              <span className="inline-flex items-center gap-2">
                <span
                  className={["h-2 w-2 shrink-0 rounded-full", wsMeta.dot].join(
                    " ",
                  )}
                  aria-hidden
                />
                <span className="text-foreground">{wsMeta.label}</span>
              </span>
            </StatusRow>

            <StatusRow label="Position Sync">
              <span className="font-mono text-foreground">
                {stablePositionSync}
              </span>
            </StatusRow>

            <div className="pt-1">
              <button
                type="button"
                onClick={handleReconcile}
                disabled={reconcileLoading}
                className="w-full rounded border border-border bg-background px-2 py-1.5 text-[11px] font-medium text-foreground hover:bg-card disabled:opacity-50"
              >
                {reconcileLoading ? "Reconciling…" : "Reconcile IG trades"}
              </button>
              {reconcileResult && (
                <p className="mt-1 text-[10px] text-muted">{reconcileResult}</p>
              )}
            </div>
          </dl>
        </Card>

        <Card title="Agent info">
          <dl className="space-y-3">
            <StatusRow label="Watchdog">
              <span
                className={
                  state?.watchdog_active ? "font-semibold text-success" : "font-semibold text-danger"
                }
              >
                {state?.watchdog_active ? "ACTIVE" : "INACTIVE"}
              </span>
            </StatusRow>

            <StatusRow label="Launchd supervision">
              <span
                className={
                  overnight.launchd_watchdog
                    ? "font-semibold text-success"
                    : "font-semibold text-warning"
                }
              >
                {overnight.launchd_watchdog ? "LOADED" : "NOT LOADED"}
              </span>
            </StatusRow>

            <StatusRow label="Overnight armed">
              <span className="font-mono text-foreground">
                {state?.overnight_armed ? "YES" : "NO"}
              </span>
            </StatusRow>

            <StatusRow label="Supervision drift">
              <span
                className={
                  state?.supervision_drift_ok === false
                    ? "font-semibold text-danger"
                    : (state?.supervision_warnings || []).length
                      ? "font-semibold text-warning"
                      : "font-semibold text-success"
                }
              >
                {state?.supervision_drift_ok === false
                  ? "ISSUES"
                  : (state?.supervision_warnings || []).length
                    ? "WARNINGS"
                    : "OK"}
              </span>
            </StatusRow>

            <StatusRow label="ML Model">
              <span className="font-mono text-foreground">
                {modelVersion ?? "—"}
              </span>
            </StatusRow>

            <StatusRow label="Last Retrain">
              <span className="tabular-nums text-foreground">
                {fmtTs(lastRetrain) ?? "Never"}
              </span>
            </StatusRow>

            <StatusRow label="Agent Uptime">
              <span className="tabular-nums text-foreground">
                {uptime ?? "—"}
              </span>
            </StatusRow>

            <StatusRow label="Last Error">
              <span
                className={[
                  "max-w-[14rem] truncate sm:max-w-none",
                  lastError ? "text-muted" : "text-muted/70",
                ].join(" ")}
                title={lastError ?? undefined}
              >
                {lastError ?? "None"}
              </span>
            </StatusRow>
          </dl>
        </Card>
      </div>

      <LearningHealthPanel />

      <ShadowVsLivePerformanceCard state={state} />

      {/* Health panel */}
      <Card title="Health">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
          <StatusRow label="Agent Uptime">
            <span className="tabular-nums">{uptime ?? "—"}</span>
          </StatusRow>
          <StatusRow label="Open Positions">
            <span className="tabular-nums">
              {state?.positions?.length ?? state?.open_positions ?? 0}
              {" / "}
              {state?.max_open_positions ?? 10}
            </span>
          </StatusRow>
          <StatusRow label="Today P&L">
            <span className={`tabular-nums font-medium ${(state?.daily_pnl_gbp ?? state?.today_pnl ?? 0) >= 0 ? "text-success" : "text-danger"}`}>
              {(() => {
                const pnl =
                  state?.daily_pnl_gbp ??
                  state?.today_pnl ??
                  (dailyLoss?.effective_loss_gbp != null
                    ? -Number(dailyLoss.effective_loss_gbp)
                    : null);
                if (pnl == null) return "—";
                const n = Number(pnl);
                return `${n >= 0 ? "+" : ""}£${Math.abs(n).toFixed(2)}`;
              })()}
            </span>
          </StatusRow>
          <StatusRow label="Points (cumul.)">
            <span className={`tabular-nums font-medium ${(state?.points?.cumulative ?? 0) >= 0 ? "text-success" : "text-danger"}`}>
              {state?.points?.cumulative != null ? `${state.points.cumulative > 0 ? "+" : ""}${Number(state.points.cumulative).toFixed(1)}` : "—"}
              {state?.points?.state ? <span className="ml-1 text-muted font-normal">({state.points.state})</span> : null}
            </span>
          </StatusRow>
          <StatusRow label="Last Gate">
            <span className="tabular-nums text-muted">
              {(() => {
                const sec = state?.last_gate_check_age_sec;
                if (sec != null && Number.isFinite(Number(sec))) {
                  const n = Math.round(Number(sec));
                  return n < 60 ? `${n}s ago` : `${Math.round(n / 60)}m ago`;
                }
                return state?.last_gate_eval_ago ?? state?.gate_eval_ago ?? "—";
              })()}
            </span>
          </StatusRow>
          <StatusRow label="Win rate (last 20)">
            <span className="tabular-nums">{state?.win_rate_20 != null ? `${Math.round(state.win_rate_20)}%` : "—"}</span>
          </StatusRow>
          {state?.drawdown?.drawdown_pct != null && (
            <StatusRow label="Drawdown">
              <span className={`tabular-nums font-medium ${Number(state.drawdown.drawdown_pct) > 3 ? "text-warning" : "text-foreground"}`}>
                {Number(state.drawdown.drawdown_pct).toFixed(1)}%
                {state.drawdown.drawdown_gbp > 0 && (
                  <span className="ml-1 text-muted font-normal">(£{Number(state.drawdown.drawdown_gbp).toFixed(0)} from peak)</span>
                )}
              </span>
            </StatusRow>
          )}
          {state?.drawdown?.max_drawdown_pct != null && state.drawdown.max_drawdown_pct > 0 && (
            <StatusRow label="Max Drawdown">
              <span className="tabular-nums text-danger">
                {Number(state.drawdown.max_drawdown_pct).toFixed(1)}% (£{Number(state.drawdown.max_drawdown_gbp).toFixed(0)})
              </span>
            </StatusRow>
          )}
        </dl>
      </Card>

      {/* ML status */}
      <Card title="ML / Learning">
        <dl className="space-y-3">
          <StatusRow label="ML Signal">
            <span className={`font-semibold uppercase ${state?.ml_enabled ? "text-success" : "text-muted"}`}>
              {state?.ml_enabled ? "Enabled" : "Disabled"}
            </span>
          </StatusRow>
          <StatusRow label="Model">
            <span className="font-mono text-foreground">{modelVersion ?? "—"}</span>
          </StatusRow>
          <StatusRow label="Last Retrain">
            <span className="tabular-nums text-foreground">{fmtTs(lastRetrain) ?? "Never"}</span>
          </StatusRow>
          <StatusRow label="Training Records (JSONL)">
            <span className={`tabular-nums font-semibold ${(state?.ml_training_records ?? 0) >= 50 ? "text-success" : "text-warning"}`}>
              {state?.ml_training_records != null ? `${state.ml_training_records} / 50` : "—"}
            </span>
          </StatusRow>
          <StatusRow label="Confirmed Trades (DB)">
            <span className="tabular-nums text-foreground">
              {state?.confirmed_trades != null ? state.confirmed_trades : "—"}
            </span>
          </StatusRow>
          <StatusRow label="OHLC Cache">
            <span className="text-foreground text-[11px]">
              {stableOhlcCount != null ? `${stableOhlcCount} markets` : "—"}
            </span>
          </StatusRow>
        </dl>
        <div className="mt-3 space-y-1">
          {(state?.ml_training_records ?? 0) === 0 && (
            <p className="text-[10px] text-warning leading-snug">
              No confirmed training records yet. Records are written when a live trade closes with IG confirmation. Keep trading — records accumulate automatically.
            </p>
          )}
          {(state?.ml_training_records ?? 0) > 0 && (state?.ml_training_records ?? 0) < 50 && (
            <p className="text-[10px] text-muted leading-snug">
              {50 - (state?.ml_training_records ?? 0)} more confirmed trades needed before ML auto-trains.
            </p>
          )}
          {(state?.ml_training_records ?? 0) >= 50 && (
            <p className="text-[10px] text-success leading-snug">
              Ready to train. Run: <code className="font-mono">PYTHONPATH=src python3 scripts/build_training_dataset.py</code>
            </p>
          )}
          <p className="text-[10px] text-muted leading-snug">
            OHLC history (Yahoo Finance) seeds the signal engine at startup.
          </p>
        </div>
      </Card>

      {/* Agent controls */}
      <Card title="Agent controls">
        <div className="flex flex-wrap gap-2 pt-1">
          <button
            type="button"
            onClick={() => openAgentModal("stop")}
            className="rounded border border-danger/60 bg-danger/10 px-4 py-2 text-[11px] font-bold uppercase tracking-wide text-danger hover:bg-danger/20"
          >
            STOP AGENT
          </button>
          <button
            type="button"
            onClick={() => openAgentModal("restart")}
            className="rounded border border-warning/60 bg-warning/10 px-4 py-2 text-[11px] font-bold uppercase tracking-wide text-warning hover:bg-warning/20"
          >
            RESTART AGENT
          </button>
          <button
            type="button"
            onClick={handleFlattenAll}
            disabled={flattenLoading}
            className="rounded border border-danger/40 px-4 py-2 text-[11px] font-semibold uppercase text-danger hover:bg-danger/10 disabled:opacity-60"
          >
            {flattenLoading ? "Closing…" : "CLOSE ALL POSITIONS"}
          </button>
        </div>
        {flattenResult && (
          <p className="mt-2 text-[12px] text-muted">{flattenResult}</p>
        )}
      </Card>

      <div className="flex flex-col items-center gap-3 pt-2">
        <button
          type="button"
          onClick={openModal}
          className="rounded border border-danger/60 bg-danger/10 px-6 py-2.5 text-[11px] font-bold uppercase tracking-wide text-danger transition-colors hover:bg-danger/20 sm:text-xs"
        >
          EMERGENCY STOP
        </button>
      </div>

      {agentModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-md rounded-lg border border-border bg-card p-4 shadow-xl sm:p-5">
            <h3 className="text-sm font-semibold text-foreground">
              {agentModal === "stop" ? "Stop Agent" : "Restart Agent"}
            </h3>
            <p className="mt-2 text-[12px] leading-snug text-muted">
              {agentModal === "stop"
                ? "All positions will be closed first, then the trading loop will stop. Type CONFIRM to proceed."
                : "All positions will be closed, then the loop restarts. Type CONFIRM to proceed."}
            </p>
            <input
              type="text"
              value={agentConfirmInput}
              onChange={(e) => setAgentConfirmInput(e.target.value)}
              placeholder="CONFIRM"
              autoComplete="off"
              disabled={agentLoading}
              className="mt-4 w-full rounded border border-border bg-bg px-3 py-2 font-mono text-sm text-foreground outline-none focus:border-warning/60 disabled:opacity-60"
            />
            {agentResult && (
              <p className="mt-3 text-[12px] font-medium text-warning">{agentResult}</p>
            )}
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button
                type="button"
                onClick={closeAgentModal}
                disabled={agentLoading}
                className="rounded border border-border px-4 py-2 text-[12px] text-muted hover:text-foreground disabled:opacity-60"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleAgentAction}
                disabled={agentConfirmInput.toUpperCase() !== "CONFIRM" || agentLoading}
                className={`rounded px-4 py-2 text-[12px] font-semibold uppercase text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-40 ${agentModal === "stop" ? "bg-danger" : "bg-warning"}`}
              >
                {agentLoading ? "Working…" : "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}

      {modalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="emergency-stop-title"
        >
          <div className="w-full max-w-md rounded-lg border border-border bg-card p-4 shadow-xl sm:p-5">
            <h3
              id="emergency-stop-title"
              className="text-sm font-semibold text-foreground"
            >
              Emergency Stop
            </h3>
            <p className="mt-2 text-[12px] leading-snug text-muted sm:text-[13px]">
              This will close all positions and lock the agent. Type STOP to
              confirm.
            </p>

            <input
              type="text"
              value={confirmInput}
              onChange={(e) => setConfirmInput(e.target.value)}
              placeholder="STOP"
              autoComplete="off"
              disabled={loading}
              className="mt-4 w-full rounded border border-border bg-bg px-3 py-2 font-mono text-sm text-foreground outline-none focus:border-danger/60 disabled:opacity-60"
            />

            {resultMsg && (
              <p className="mt-3 text-[12px] font-medium text-danger">
                {resultMsg}
              </p>
            )}

            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button
                type="button"
                onClick={closeModal}
                disabled={loading}
                className="rounded border border-border px-4 py-2 text-[12px] text-muted transition-colors hover:text-foreground disabled:opacity-60"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirm}
                disabled={confirmInput !== "STOP" || loading}
                className="rounded bg-danger px-4 py-2 text-[12px] font-semibold uppercase text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-40"
              >
                {loading ? "Stopping…" : "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
