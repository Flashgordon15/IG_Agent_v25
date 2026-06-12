import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client.js";
import {
  epicShortLabel,
  resolveActiveEpics,
} from "../utils/roadmapTelemetry.js";

const RISK_POLL_MS = 30_000;

function resolveAllPositions(state) {
  if (Array.isArray(state?.positions) && state.positions.length > 0) {
    return state.positions;
  }
  const markets = state?.markets;
  if (!markets || typeof markets !== "object") return [];
  const all = [];
  for (const [epic, slice] of Object.entries(markets)) {
    const rows = slice?.positions;
    if (!Array.isArray(rows)) continue;
    rows.forEach((p) => {
      all.push({ epic, market: p.market ?? slice?.market ?? epic, ...p });
    });
  }
  return all;
}

function resolveInterventionEpics(state, positions) {
  const rotation = resolveActiveEpics(state);
  if (rotation.length) return rotation;
  const fromPositions = [...new Set(positions.map((p) => p.epic).filter(Boolean))];
  if (fromPositions.length) return fromPositions;
  if (Array.isArray(state?.enabled_epics)) {
    return state.enabled_epics.filter(Boolean);
  }
  return [];
}

function positionsForEpic(positions, epic) {
  return positions.filter((p) => p.epic === epic);
}

/** Stable fingerprint — memo re-render only when position lifecycle fields change. */
export function positionStateFingerprint(positions, epic) {
  const rows = positionsForEpic(positions, epic);
  if (!rows.length) return `${epic}|flat`;
  return rows
    .map((p) =>
      [
        p.deal_id ?? "",
        p.side ?? "",
        Number(p.entry ?? 0).toFixed(5),
        Number(p.stop ?? 0).toFixed(5),
        Number(p.current ?? 0).toFixed(5),
        Number(p.pnl_gbp ?? 0).toFixed(2),
        p.breakeven_hit ? "1" : "0",
        p.trail_active ? "1" : "0",
      ].join(":"),
    )
    .sort()
    .join("|");
}

function shieldRingMeta(riskStatus) {
  const breached = Boolean(
    riskStatus?.daily_max_loss_breached || riskStatus?.entries_blocked_by_shield,
  );
  if (breached) {
    return {
      breached: true,
      ring: "border-[#be123c] shadow-[0_0_18px_rgba(190,18,60,0.45)]",
      dot: "bg-[#be123c]",
      label: "SHIELD TRIPPED",
      sub: riskStatus?.shield?.detail || "Entries blocked until midnight",
    };
  }
  return {
    breached: false,
    ring: "border-success shadow-[0_0_14px_rgba(34,197,94,0.35)]",
    dot: "bg-success animate-pulse",
    label: "TRADING ALLOWED",
    sub: riskStatus?.shield?.detail || "Daily drawdown shield clear",
  };
}

function DrawdownShieldRing({ riskStatus }) {
  const meta = shieldRingMeta(riskStatus);
  const threshold = riskStatus?.shield?.threshold_gbp;
  const closedLoss = riskStatus?.shield?.closed_loss_gbp;

  return (
    <div className="flex items-center gap-4">
      <div
        className={[
          "relative flex h-16 w-16 shrink-0 items-center justify-center rounded-full border-[3px] bg-card",
          meta.ring,
        ].join(" ")}
        aria-hidden
      >
        <span className={`h-4 w-4 rounded-full ${meta.dot}`} />
      </div>
      <div className="min-w-0 flex-1">
        <p className="label-caps">Daily drawdown shield</p>
        <p
          className={[
            "mt-0.5 text-sm font-bold uppercase tracking-wide",
            meta.breached ? "text-[#f87171]" : "text-success",
          ].join(" ")}
        >
          {meta.label}
        </p>
        <p className="mt-1 text-[11px] leading-snug text-muted">{meta.sub}</p>
        {threshold != null && (
          <p className="mt-1 text-[10px] tabular-nums text-muted">
            Closed loss £{Number(closedLoss ?? 0).toFixed(0)} / £{Number(threshold).toFixed(0)} threshold
          </p>
        )}
      </div>
    </div>
  );
}

const MemoDrawdownShieldRing = memo(
  DrawdownShieldRing,
  (prev, next) =>
    prev.riskStatus?.daily_max_loss_breached === next.riskStatus?.daily_max_loss_breached
    && prev.riskStatus?.entries_blocked_by_shield === next.riskStatus?.entries_blocked_by_shield
    && prev.riskStatus?.shield?.closed_loss_gbp === next.riskStatus?.shield?.closed_loss_gbp
    && prev.riskStatus?.shield?.detail === next.riskStatus?.shield?.detail
    && prev.riskStatus?.shield?.threshold_gbp === next.riskStatus?.shield?.threshold_gbp,
);

function EpicInterventionRow({ epic, label, positions, onRefreshRisk }) {
  const epicPositions = positionsForEpic(positions, epic);
  const hasPosition = epicPositions.length > 0;
  const primary = epicPositions[0];
  const [closeStep, setCloseStep] = useState(0);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    setCloseStep(0);
    setMessage("");
  }, [primary?.deal_id, primary?.stop, primary?.entry]);

  const runAction = useCallback(
    async (action, fn) => {
      setBusy(action);
      setMessage("");
      try {
        const result = await fn();
        setMessage(result?.ok === false ? "Action reported errors — check logs" : "Done");
        onRefreshRisk?.();
      } catch (err) {
        setMessage(err?.message || "Request failed");
      } finally {
        setBusy("");
        setCloseStep(0);
      }
    },
    [onRefreshRisk],
  );

  return (
    <div
      className={[
        "rounded-lg border px-3 py-3",
        hasPosition ? "border-border bg-surface" : "border-border/60 bg-card/40 opacity-70",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[12px] font-bold uppercase tracking-wide text-foreground">
            {label}
          </p>
          <p className="mt-0.5 truncate font-mono text-[10px] text-muted">{epic}</p>
          {hasPosition ? (
            <p className="mt-1 text-[11px] text-muted">
              {primary.side} · entry {Number(primary.entry).toFixed(2)} · stop{" "}
              {Number(primary.stop).toFixed(2)}
              {primary.pnl_gbp != null && (
                <span
                  className={
                    Number(primary.pnl_gbp) >= 0 ? " text-success" : " text-danger"
                  }
                >
                  {" "}
                  · £{Number(primary.pnl_gbp).toFixed(2)}
                </span>
              )}
            </p>
          ) : (
            <p className="mt-1 text-[11px] text-muted">No open position</p>
          )}
        </div>
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
          <button
            type="button"
            disabled={!hasPosition || !!busy}
            onClick={() => {
              if (closeStep < 1) {
                setCloseStep(1);
                return;
              }
              runAction("close", () => api.adminForceClose(epic));
            }}
            className={[
              "min-w-[9.5rem] rounded-md px-3 py-2 text-[11px] font-extrabold uppercase tracking-wide transition-colors disabled:cursor-not-allowed disabled:opacity-40",
              closeStep === 1
                ? "bg-danger text-white ring-2 ring-danger/60"
                : "border-2 border-danger bg-danger/15 text-danger hover:bg-danger/25",
            ].join(" ")}
          >
            {busy === "close"
              ? "Closing…"
              : closeStep === 1
                ? "Confirm force close"
                : "Force close"}
          </button>
          <button
            type="button"
            disabled={!hasPosition || !!busy}
            onClick={() => runAction("breakeven", () => api.adminForceBreakeven(epic))}
            className="min-w-[9.5rem] rounded-md border-2 border-amber-400 bg-amber-400/20 px-3 py-2 text-[11px] font-extrabold uppercase tracking-wide text-amber-300 transition-colors hover:bg-amber-400/30 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy === "breakeven" ? "Locking…" : "Lock breakeven"}
          </button>
        </div>
      </div>
      {closeStep === 1 && !busy && (
        <button
          type="button"
          className="mt-2 text-[10px] text-muted underline-offset-2 hover:underline"
          onClick={() => setCloseStep(0)}
        >
          Cancel force close
        </button>
      )}
      {message && <p className="mt-2 text-[11px] text-muted">{message}</p>}
    </div>
  );
}

const MemoEpicInterventionRow = memo(
  EpicInterventionRow,
  (prev, next) =>
    prev.epic === next.epic
    && prev.label === next.label
    && positionStateFingerprint(prev.positions, prev.epic)
      === positionStateFingerprint(next.positions, next.epic),
);

function ManualInterventionControlCard({ state }) {
  const [riskStatus, setRiskStatus] = useState(null);
  const [riskError, setRiskError] = useState("");

  const labels = state?.instrument_labels && typeof state.instrument_labels === "object"
    ? state.instrument_labels
    : {};
  const positions = useMemo(() => resolveAllPositions(state), [state]);
  const rotationEpics = useMemo(
    () => resolveInterventionEpics(state, positions),
    [state, positions],
  );

  const refreshRisk = useCallback(async () => {
    try {
      const payload = await api.adminRiskStatus();
      setRiskStatus(payload);
      setRiskError("");
    } catch (err) {
      setRiskError(err?.message || "Risk status unavailable");
    }
  }, []);

  useEffect(() => {
    refreshRisk();
    const timer = window.setInterval(refreshRisk, RISK_POLL_MS);
    return () => window.clearInterval(timer);
  }, [refreshRisk]);

  return (
    <section className="rounded-lg border border-border bg-card p-3 sm:p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="label-caps">Manual execution</h2>
          <p className="mt-1 text-[11px] leading-snug text-muted">
            Admin overrides — bypass trailing delays. Force close requires confirmation.
          </p>
        </div>
      </div>

      <MemoDrawdownShieldRing riskStatus={riskStatus} />

      {riskError && (
        <p className="mt-3 text-[11px] text-warning">{riskError}</p>
      )}

      <div className="mt-4 space-y-2">
        <p className="label-caps">Rotation assets</p>
        {rotationEpics.length === 0 ? (
          <p className="text-[11px] text-muted">No rotation epics in snapshot.</p>
        ) : (
          rotationEpics.map((epic) => (
            <MemoEpicInterventionRow
              key={epic}
              epic={epic}
              label={epicShortLabel(epic, labels)}
              positions={positions}
              onRefreshRisk={refreshRisk}
            />
          ))
        )}
      </div>
    </section>
  );
}

function manualInterventionCardPropsEqual(prev, next) {
  const prevEpics = resolveInterventionEpics(prev.state, resolveAllPositions(prev.state));
  const nextEpics = resolveInterventionEpics(next.state, resolveAllPositions(next.state));
  if (prevEpics.join("|") !== nextEpics.join("|")) return false;

  const prevPositions = resolveAllPositions(prev.state);
  const nextPositions = resolveAllPositions(next.state);
  for (const epic of nextEpics) {
    if (
      positionStateFingerprint(prevPositions, epic)
      !== positionStateFingerprint(nextPositions, epic)
    ) {
      return false;
    }
  }
  return true;
}

export default memo(ManualInterventionControlCard, manualInterventionCardPropsEqual);
