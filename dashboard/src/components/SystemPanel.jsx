import { useState } from "react";
import { postEmergencyStop } from "../api.js";

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
  if (!ts) return null;
  try {
    return new Date(ts).toLocaleString("en-GB", {
      dateStyle: "short",
      timeStyle: "medium",
    });
  } catch {
    return String(ts);
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

function restBudgetMeta(used) {
  if (used <= 3) {
    return { bar: "bg-success", text: "text-success" };
  }
  if (used <= 5) {
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
  const [modalOpen, setModalOpen] = useState(false);
  const [confirmInput, setConfirmInput] = useState("");
  const [resultMsg, setResultMsg] = useState("");
  const [loading, setLoading] = useState(false);

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
  const uptime = resolveUptime(state);

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">
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
                  {restUsed} / 6
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded bg-border">
                <div
                  className={["h-full transition-all duration-300", restMeta.bar].join(
                    " ",
                  )}
                  style={{ width: `${Math.min(100, (restUsed / 6) * 100)}%` }}
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
                {resolvePositionSyncStatus(state)}
              </span>
            </StatusRow>
          </dl>
        </Card>

        <Card title="Agent info">
          <dl className="space-y-3">
            <StatusRow label="Model Version">
              <span className="font-mono text-foreground">
                {modelVersion ?? "No model"}
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

      <div className="flex flex-col items-center gap-3 pt-2">
        <button
          type="button"
          onClick={openModal}
          className="rounded border border-danger/60 bg-danger/10 px-6 py-2.5 text-[11px] font-bold uppercase tracking-wide text-danger transition-colors hover:bg-danger/20 sm:text-xs"
        >
          EMERGENCY STOP
        </button>
      </div>

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
