/**
 * SystemHealthWidget — polls /api/health_light every 2s.
 * Shows: Agent Online, Execution Loop, Routing Armed/Degraded,
 *        Feed Age, WS State, API Latency, IG/Yahoo availability.
 */

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { fetchHealthLight } from "../lib/api";
import { cn } from "../lib/utils";

interface HealthLightData {
  agent_online: boolean;
  execution_loop_active: boolean;
  routing_state: { armed: number; degraded: boolean; none: number };
  feed_heartbeat_age_ms: number | null;
  ws_state: { connected: boolean; degraded: boolean; reconnecting: boolean };
  cached_api_latency_ms: number | null;
  ig_available: boolean | null;
  yahoo_available: boolean | null;
  heartbeat_ts: string;
  agent_version: string;
  feed_stall?: boolean;
  rotation_escape_active?: boolean;
  last_rotation_reason?: string;
  stack_tpm?: Record<string, number>;
  rotation_sweep_count?: number;
  stacked_sweep_alive?: boolean;
  z_stream_lengths?: Record<string, number>;
}

function dot(ok: boolean | null, warn?: boolean): string {
  if (ok === null) return "bg-slate-600";
  if (warn) return "bg-amber-400";
  return ok ? "bg-emerald-400" : "bg-red-500";
}

function feedAgeLabel(ms: number | null): { label: string; warn: boolean } {
  if (ms === null) return { label: "–", warn: true };
  const s = ms / 1000;
  if (s > 10) return { label: `${s.toFixed(1)}s`, warn: true };
  return { label: `${s.toFixed(1)}s`, warn: false };
}

function HealthRow({
  label,
  ok,
  value,
  warn,
}: {
  label: string;
  ok: boolean | null;
  value?: string;
  warn?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-2 py-0.5">
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            "inline-block h-1.5 w-1.5 rounded-full transition-colors duration-500",
            dot(ok, warn),
          )}
        />
        <span className="text-[10px] uppercase tracking-wider text-slate-400">
          {label}
        </span>
      </div>
      {value !== undefined && (
        <span
          className={cn(
            "font-mono text-[10px]",
            ok === null ? "text-slate-500" : warn ? "text-amber-400" : ok ? "text-emerald-400" : "text-red-400",
          )}
        >
          {value}
        </span>
      )}
    </div>
  );
}

function Badge({
  ok,
  label,
}: {
  ok: boolean | null;
  label: string;
}) {
  return (
    <span
      className={cn(
        "rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest",
        ok === null
          ? "bg-slate-700 text-slate-400"
          : ok
            ? "bg-emerald-900/60 text-emerald-300"
            : "bg-red-900/50 text-red-400",
      )}
    >
      {label}
    </span>
  );
}

export default memo(function SystemHealthWidget() {
  const [data, setData] = useState<HealthLightData | null>(null);
  const [stale, setStale] = useState(false);
  const lastGoodRef = useRef<HealthLightData | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const poll = useCallback(async () => {
    try {
      const result = await fetchHealthLight();
      const typed = result as unknown as HealthLightData;
      lastGoodRef.current = typed;
      setData(typed);
      setStale(false);
    } catch {
      // Show last good values while polling fails
      if (lastGoodRef.current) {
        setStale(true);
      }
    }
  }, []);

  useEffect(() => {
    void poll();
    intervalRef.current = setInterval(() => void poll(), 2000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [poll]);

  const display = data ?? lastGoodRef.current;
  const { label: feedLabel, warn: feedWarn } = feedAgeLabel(
    display?.feed_heartbeat_age_ms ?? null,
  );

  const wsOk =
    display?.ws_state.connected === true
      ? true
      : display?.ws_state.reconnecting
        ? null
        : false;

  const latencyMs = display?.cached_api_latency_ms ?? null;
  const latencyWarn = latencyMs !== null && latencyMs > 50;

  return (
    <div
      className={cn(
        "panel flex flex-col gap-1 p-2 transition-opacity duration-300",
        stale && "opacity-60",
      )}
    >
      <div className="flex items-center justify-between">
        <span className="panel-header mb-0 text-[9px] uppercase tracking-widest text-slate-500">
          System Health
        </span>
        {display?.agent_version && (
          <span className="text-[9px] text-slate-600">
            v{display.agent_version}
          </span>
        )}
      </div>

      <div className="mt-1 space-y-0.5">
        <HealthRow
          label="Agent Online"
          ok={display?.agent_online ?? null}
          value={display ? "LIVE" : "–"}
        />
        <HealthRow
          label="Exec Loop"
          ok={display?.execution_loop_active ?? null}
          value={
            display?.execution_loop_active
              ? `ACTIVE · ${display.rotation_sweep_count ?? 0} sweeps`
              : display?.stacked_sweep_alive === false
                ? "THREAD DOWN"
                : "IDLE"
          }
        />
        <HealthRow
          label="Routing"
          ok={
            display
              ? display.routing_state.armed > 0 &&
                !display.routing_state.degraded
              : null
          }
          value={
            display
              ? display.routing_state.degraded
                ? "DEGRADED"
                : display.routing_state.armed > 0
                  ? `${display.routing_state.armed} ARMED`
                  : "NONE"
              : "–"
          }
          warn={display?.routing_state.degraded}
        />
        <HealthRow
          label="Feed Age"
          ok={display ? !feedWarn && !display.feed_stall : null}
          value={feedLabel}
          warn={feedWarn || display?.feed_stall}
        />
        {display?.feed_stall && (
          <HealthRow label="Feed Stall" ok={false} value="STALL" />
        )}
        {display?.z_stream_lengths && Object.keys(display.z_stream_lengths).length > 0 ? (
          <div className="text-[9px] text-slate-500 truncate" title={JSON.stringify(display.z_stream_lengths)}>
            Z stream: {Object.values(display.z_stream_lengths).join("/")}
          </div>
        ) : null}
        {display?.last_rotation_reason ? (
          <div className="truncate text-[9px] text-slate-500 py-0.5" title={display.last_rotation_reason}>
            ↻ {display.last_rotation_reason}
          </div>
        ) : null}
        <HealthRow
          label="WS"
          ok={wsOk}
          value={
            display?.ws_state.reconnecting
              ? "RECONNECTING"
              : display?.ws_state.connected
                ? "CONNECTED"
                : display
                  ? "DISCONNECTED"
                  : "–"
          }
        />
        <HealthRow
          label="API Latency"
          ok={latencyMs !== null ? !latencyWarn : null}
          value={latencyMs !== null ? `${latencyMs.toFixed(1)}ms` : "–"}
          warn={latencyWarn}
        />
      </div>

      <div className="mt-1.5 flex gap-1.5 border-t border-slate-800 pt-1.5">
        <Badge ok={display?.ig_available ?? null} label="IG" />
        <Badge ok={display?.yahoo_available ?? null} label="Yahoo" />
        {stale && (
          <span className="ml-auto text-[9px] italic text-amber-500">
            stale
          </span>
        )}
      </div>

      {display?.heartbeat_ts && (
        <div className="text-[8px] text-slate-700 tabular-nums">
          {display.heartbeat_ts.slice(11, 19)} UTC
        </div>
      )}
    </div>
  );
});
