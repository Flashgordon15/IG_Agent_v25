/**
 * Unified trading panels — lifecycle, rejections, rotation, stops/limits.
 * Polls /api/unified_status and related endpoints.
 */

import { memo, useCallback, useEffect, useState } from "react";
import {
  fetchRejections,
  fetchRotationStatus,
  fetchTradeLifecycle,
  fetchUnifiedStatus,
} from "../lib/api";
import { cn } from "../lib/utils";

interface LifecycleTrade {
  deal_id: string;
  epic: string;
  direction: string;
  state: string;
  size?: number;
  last_message?: string;
}

interface RejectionRow {
  epic: string;
  reason: string;
  classification: string;
  self_correction_attempted?: boolean;
  ts: string;
}

function Section({
  title,
  children,
  className,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-lg border border-border/60 bg-panel/50 p-2", className)}>
      <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
        {title}
      </h3>
      {children}
    </div>
  );
}

function stateColor(state: string): string {
  const s = state.toUpperCase();
  if (s === "ACTIVE" || s === "CONFIRMED") return "text-emerald-400";
  if (s === "REJECTED" || s === "CLOSING") return "text-red-400";
  if (s === "ARMED_STOP" || s === "PENDING") return "text-amber-400";
  return "text-slate-400";
}

function UnifiedTradingPanelsInner() {
  const [lifecycle, setLifecycle] = useState<LifecycleTrade[]>([]);
  const [rejections, setRejections] = useState<RejectionRow[]>([]);
  const [rotation, setRotation] = useState<Record<string, unknown>>({});
  const [stops, setStops] = useState<Record<string, unknown>>({});
  const [sizing, setSizing] = useState<Record<string, unknown>>({});
  const [exec, setExec] = useState<Record<string, unknown>>({});

  const refresh = useCallback(async () => {
    try {
      const [unified, lc, rej, rot] = await Promise.all([
        fetchUnifiedStatus(),
        fetchTradeLifecycle(),
        fetchRejections(10),
        fetchRotationStatus(),
      ]);
      const machine = (lc.machine as { active?: Record<string, LifecycleTrade> })?.active ?? {};
      setLifecycle(Object.values(machine));
      setRejections(
        ((rej.rejections as RejectionRow[]) ?? []).slice(0, 5),
      );
      setRotation((rot.rotation as Record<string, unknown>) ?? {});
      setStops((unified.stops_limits as Record<string, unknown>) ?? {});
      setSizing((unified.sizing as Record<string, unknown>) ?? {});
      setExec((unified.execution as Record<string, unknown>) ?? {});
    } catch {
      /* non-blocking */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 3000);
    return () => clearInterval(id);
  }, [refresh]);

  const perTrade = (stops.per_trade as Record<string, Record<string, unknown>>) ?? {};
  const lastValidation =
    (sizing.last_validation as Record<string, { ok?: boolean; adjusted_size?: number }>) ?? {};

  return (
    <div className="flex min-h-0 flex-col gap-2 overflow-y-auto">
      <Section title="Trade Lifecycle">
        {lifecycle.length === 0 ? (
          <p className="text-[10px] text-muted">No active trades</p>
        ) : (
          <ul className="space-y-1">
            {lifecycle.map((t) => (
              <li key={t.deal_id} className="flex justify-between gap-2 text-[10px]">
                <span className="truncate text-text-secondary">
                  {t.epic?.slice(-12)} {t.direction}
                </span>
                <span className={cn("shrink-0 font-mono", stateColor(t.state))}>
                  {t.state}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-1 text-[9px] text-muted">
          Loop: {exec.loop_active ? "active" : "idle"}
          {exec.last_dispatch_at ? ` · last ${String(exec.last_dispatch_at).slice(11, 19)}` : ""}
        </p>
      </Section>

      <Section title="IG Rejections">
        {rejections.length === 0 ? (
          <p className="text-[10px] text-emerald-400/80">None recent</p>
        ) : (
          <ul className="space-y-1">
            {rejections.map((r, i) => (
              <li key={`${r.ts}-${i}`} className="text-[10px]">
                <span className="text-red-400">{r.classification}</span>
                <span className="text-muted"> · {r.epic?.slice(-10)}</span>
                {r.self_correction_attempted ? (
                  <span className="text-amber-400"> · retried</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Rotation">
        <div className="space-y-0.5 text-[10px] text-text-secondary">
          <p>
            Sweeps:{" "}
            <span className="font-mono text-accent">
              {String(rotation.rotation_sweep_count ?? "—")}
            </span>
          </p>
          <p className="truncate">
            Focus:{" "}
            <span className="font-mono">
              {String(rotation.execution_focus_epic ?? rotation.current_epic ?? "—")}
            </span>
          </p>
          {rotation.last_rotation_reason ? (
            <p className="truncate text-muted">{String(rotation.last_rotation_reason)}</p>
          ) : null}
        </div>
      </Section>

      <Section title="Stops / Limits / Size">
        <div className="space-y-0.5 text-[10px]">
          <p>
            Trailing:{" "}
            <span
              className={
                stops.trailing_stop_engine_active ? "text-emerald-400" : "text-muted"
              }
            >
              {stops.trailing_stop_engine_active ? "active" : "off"}
            </span>
            {" · "}
            Dynamic:{" "}
            <span
              className={
                stops.dynamic_limit_engine_active ? "text-emerald-400" : "text-muted"
              }
            >
              {stops.dynamic_limit_engine_active ? "active" : "off"}
            </span>
          </p>
          {Object.keys(perTrade).length > 0 ? (
            <ul className="mt-1 space-y-0.5">
              {Object.entries(perTrade)
                .slice(0, 3)
                .map(([id, row]) => (
                  <li key={id} className="truncate text-muted">
                    {String(row.epic ?? id).slice(-12)} ceiling{" "}
                    {String(row.ceiling_pts ?? "2")}pt
                  </li>
                ))}
            </ul>
          ) : null}
          {Object.keys(lastValidation).length > 0 ? (
            <p className="mt-1 truncate text-muted">
              Size OK:{" "}
              {Object.entries(lastValidation)
                .slice(0, 2)
                .map(([ep, v]) => `${ep.slice(-8)}=${v.adjusted_size}`)
                .join(", ")}
            </p>
          ) : null}
        </div>
      </Section>
    </div>
  );
}

export default memo(UnifiedTradingPanelsInner);
