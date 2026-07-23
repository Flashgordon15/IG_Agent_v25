"use client";

import { memo, useCallback, useEffect, useState } from "react";
import { SystemBlueprintModal } from "@/components/SystemBlueprintModal";
import { agentHttpBase, fetchAgentJson } from "@/lib/agent-client";
import type { WsState } from "@/hooks/useAgentTelemetry";

export type EngineRuntime = "ACTIVE" | "IDLE" | "PAUSED" | "ERROR";

type DeskStability = {
  grade?: string;
  label?: string;
  reasons?: string[];
};

type OpsStrip = {
  ok?: boolean;
  daily_realized_pnl_gbp?: number;
  daily_milestone_gbp?: number;
  progress_pct?: number;
  atr_reward_risk?: number;
  grok_macro_bias?: string;
  trading_path_live?: boolean;
  trading_path_badge?: string;
  trading_path_blockers?: string[];
  desk_rag?: string;
  desk_rag_label?: string;
  cap_breach?: boolean;
  cap_breach_warning?: string;
  rest_pressure?: boolean;
  rest_pressure_level?: string;
  broker_open_snapshot?: number | null;
  max_open_positions?: number;
  broker_open_sot?: {
    count?: number | null;
    source?: string;
    ok?: boolean;
    status_age_sec?: number;
  };
  desk_liveness?: {
    ok?: boolean | null;
    has_open_risk?: boolean | null;
    open_count?: number;
  };
  feed_transport_summary?: {
    label?: string;
    streaming_transport?: string;
    primary_quote_feed?: string;
  };
  desk_stability?: DeskStability;
};

type Props = {
  engineRuntime: EngineRuntime;
  tradingHealthy: boolean | null;
  positionsVerdict: string;
  wsState: WsState;
  feedStale: boolean;
};

function engineTone(runtime: EngineRuntime): string {
  if (runtime === "ACTIVE") return "text-[var(--color-desk-good)]";
  if (runtime === "IDLE") return "text-[var(--color-desk-mute)]";
  if (runtime === "PAUSED") return "text-[var(--color-desk-warn)]";
  return "text-[var(--color-desk-bad)]";
}

function engineDot(runtime: EngineRuntime): string {
  if (runtime === "ACTIVE") return "cq-status-dot cq-status-dot-live";
  if (runtime === "ERROR") return "cq-status-dot cq-status-dot-danger";
  return "cq-status-dot cq-status-dot-warn";
}

function feedLabel(wsState: WsState, feedStale: boolean): string {
  if (wsState === "live" && !feedStale) return "LIVE";
  if (wsState === "stale" || feedStale) return "STALE";
  if (wsState === "connecting") return "CONNECTING";
  return "OFFLINE";
}

function feedTone(wsState: WsState, feedStale: boolean): string {
  if (wsState === "live" && !feedStale) return "text-[var(--color-desk-good)]";
  if (wsState === "connecting") return "text-[var(--color-desk-mute)]";
  return "text-[var(--color-desk-warn)]";
}

function feedDot(wsState: WsState, feedStale: boolean): string {
  if (wsState === "live" && !feedStale) return "cq-status-dot cq-status-dot-live";
  if (wsState === "connecting") return "cq-status-dot bg-[var(--color-desk-faint)]";
  return "cq-status-dot cq-status-dot-warn";
}

function ragTone(rag: string): string {
  if (rag === "G") return "text-[var(--color-desk-good)]";
  if (rag === "R") return "text-[var(--color-desk-bad)]";
  return "text-[var(--color-desk-warn)]";
}

function ragDot(rag: string): string {
  if (rag === "G") return "cq-status-dot cq-status-dot-live";
  if (rag === "R") return "cq-status-dot cq-status-dot-danger";
  return "cq-status-dot cq-status-dot-warn";
}

function StatusCard({
  label,
  value,
  valueClass,
  dotClass,
  sub,
}: {
  label: string;
  value: string;
  valueClass: string;
  dotClass: string;
  sub: string;
}) {
  return (
    <div className="min-w-[8.5rem] rounded-[calc(var(--desk-radius)-2px)] border border-[var(--color-desk-line)] bg-[var(--color-desk-elevated)]/80 px-3 py-2.5">
      <div className="cq-label mb-1.5">{label}</div>
      <div className="flex items-center gap-2">
        <span className={dotClass} />
        <span className={`cq-mono text-[length:clamp(0.95rem,0.85rem+0.25vw,1.15rem)] font-semibold ${valueClass}`}>
          {value}
        </span>
      </div>
      <div className="cq-mono mt-1 text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
        {sub}
      </div>
    </div>
  );
}

function DeskStatusBarImpl({
  engineRuntime,
  tradingHealthy,
  positionsVerdict,
  wsState,
  feedStale,
}: Props) {
  const [killing, setKilling] = useState(false);
  const [killMsg, setKillMsg] = useState<string | null>(null);
  const [ops, setOps] = useState<OpsStrip | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const strip = await fetchAgentJson<OpsStrip>(
          "/api/desk/ops_strip",
          undefined,
          2500,
        );
        if (!cancelled) setOps(strip);
      } catch {
        if (!cancelled) setOps(null);
      }
    };
    void poll();
    const id = window.setInterval(poll, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const onKill = useCallback(async () => {
    const ok = window.confirm(
      "EMERGENCY BOT KILL SWITCH\n\nStop trading loops, cancel working orders, and flatten ALL live broker positions?",
    );
    if (!ok) return;
    setKilling(true);
    setKillMsg(null);
    try {
      const res = await fetch(`${agentHttpBase()}/api/v1/emergency/kill`, {
        method: "POST",
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(45000),
      });
      const body = (await res.json().catch(() => ({}))) as {
        ok?: boolean;
        closed?: string[];
        errors?: string[];
        error?: string;
      };
      if (!res.ok || body.ok === false) {
        setKillMsg(
          body.error ||
            (body.errors && body.errors[0]) ||
            `kill HTTP ${res.status}`,
        );
      } else {
        setKillMsg(
          `KILL OK — closed ${(body.closed || []).length} · errors ${(body.errors || []).length}`,
        );
      }
    } catch (e) {
      setKillMsg(e instanceof Error ? e.message : "kill failed");
    } finally {
      setKilling(false);
    }
  }, []);

  const verdict = (positionsVerdict || "—").toUpperCase();
  // Never greenwash ENGINE from health.ok alone — path + stability win.
  const pathLive = ops?.trading_path_live === true;
  const rag = String(ops?.desk_rag || ops?.desk_stability?.grade || "A").toUpperCase();
  const stabilityGrade = String(
    ops?.desk_stability?.grade || rag || "A",
  ).toUpperCase();
  const execLabel =
    tradingHealthy === null
      ? "UNKNOWN"
      : !pathLive
        ? "PATH DOWN"
        : tradingHealthy
          ? "HEALTHY"
          : "DEGRADED";
  const milestoneGbp = Number(ops?.daily_milestone_gbp ?? 1000);
  const realized = Number(ops?.daily_realized_pnl_gbp ?? 0);
  const progress = Number(ops?.progress_pct ?? 0);
  const atrRr = Number(ops?.atr_reward_risk ?? 3.5);
  const bias = String(ops?.grok_macro_bias || "—").toUpperCase();
  const pathBadge = String(ops?.trading_path_badge || "").trim();
  const capBreach = ops?.cap_breach === true;
  const restHot = ops?.rest_pressure === true;
  const sot = ops?.broker_open_sot;
  const sotCount =
    sot?.count != null ? Number(sot.count) : ops?.broker_open_snapshot ?? null;
  const sotOk = sot?.ok !== false;
  const liv = ops?.desk_liveness;
  const livOk = liv?.ok;
  const feedSummary =
    ops?.feed_transport_summary?.label ||
    [
      ops?.feed_transport_summary?.streaming_transport,
      ops?.feed_transport_summary?.primary_quote_feed,
    ]
      .filter(Boolean)
      .join(" · ") ||
    "WS telem. ≠ engine";
  const bookSubBits = [
    `SoT ${sotCount ?? "—"}`,
    livOk === false ? "LIV↓" : livOk === true ? "LIV✓" : null,
    pathLive ? "PATH LIVE" : pathBadge ? "PATH DOWN" : null,
    capBreach ? "CAP" : null,
    restHot ? `REST ${ops?.rest_pressure_level || "HOT"}` : null,
  ].filter(Boolean);
  const stabilitySub = (
    ops?.desk_stability?.reasons?.[0] ||
    ops?.desk_rag_label ||
    `RAG ${rag}`
  ).slice(0, 42);

  return (
    <div
      className="cq-panel flex flex-col gap-3"
      style={{ padding: "0.75rem clamp(0.85rem, 1.2vw, 1.25rem)" }}
    >
      <div className="flex flex-wrap items-stretch justify-between gap-3">
        <div className="flex flex-wrap items-stretch gap-2.5">
          <StatusCard
            label="STABILITY"
            value={stabilityGrade}
            valueClass={ragTone(stabilityGrade)}
            dotClass={ragDot(stabilityGrade)}
            sub={stabilitySub}
          />
          <StatusCard
            label="ENGINE"
            value={engineRuntime}
            valueClass={
              pathLive && engineRuntime === "ACTIVE"
                ? engineTone(engineRuntime)
                : "text-[var(--color-desk-warn)]"
            }
            dotClass={
              pathLive && engineRuntime === "ACTIVE"
                ? engineDot(engineRuntime)
                : "cq-status-dot cq-status-dot-warn"
            }
            sub={`loops · ${execLabel}`}
          />
          <StatusCard
            label="FEED"
            value={feedLabel(wsState, feedStale)}
            valueClass={feedTone(wsState, feedStale)}
            dotClass={feedDot(wsState, feedStale)}
            sub={feedSummary.slice(0, 48)}
          />
          <StatusCard
            label="BOOK"
            value={verdict}
            valueClass={
              verdict === "CRITICAL" || !sotOk
                ? "text-[var(--color-desk-bad)]"
                : verdict === "HEALTHY" || verdict === "FLAT"
                  ? "text-[var(--color-desk-good)]"
                  : "text-[var(--color-desk-warn)]"
            }
            dotClass={
              verdict === "HEALTHY" || verdict === "FLAT"
                ? "cq-status-dot cq-status-dot-live"
                : verdict === "CRITICAL"
                  ? "cq-status-dot cq-status-dot-danger"
                  : "cq-status-dot cq-status-dot-warn"
            }
            sub={bookSubBits.join(" · ") || `bias ${bias}`}
          />
          <div className="min-w-[8.5rem] rounded-[calc(var(--desk-radius)-2px)] border border-[rgba(61,220,151,0.22)] bg-[rgba(61,220,151,0.06)] px-3 py-2.5">
            <div className="cq-label mb-1.5 text-[var(--color-desk-good)]">
              ATR TARGET
            </div>
            <div className="cq-mono text-[length:clamp(0.95rem,0.85rem+0.25vw,1.15rem)] font-semibold text-[var(--color-desk-good)]">
              {atrRr.toFixed(1)}× R:R
            </div>
            <div className="cq-mono mt-1 text-[length:var(--desk-type-xs)] text-[rgba(61,220,151,0.7)]">
              elevated-vol bracket
            </div>
          </div>
        </div>

        <div className="flex min-w-[18rem] flex-1 flex-col justify-center gap-2 rounded-[calc(var(--desk-radius)-2px)] border border-[var(--color-desk-line)] bg-[var(--color-desk-elevated)]/80 px-3 py-2.5">
          <div className="flex items-center justify-between gap-2">
            <span className="cq-label">£1,000 Daily Milestone</span>
            <span className="cq-mono text-[length:var(--desk-type-sm)] text-[var(--color-desk-mute)]">
              {progress.toFixed(1)}%
            </span>
          </div>
          <div className="cq-progress">
            <i style={{ width: `${Math.min(100, Math.max(0, progress))}%` }} />
          </div>
          <div className="flex justify-between cq-mono text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
            <span>
              {realized >= 0 ? "+" : ""}£{realized.toFixed(2)} realized
            </span>
            <span>target £{milestoneGbp.toFixed(0)}</span>
          </div>
        </div>

        <div className="flex flex-col items-stretch justify-center gap-2 self-center">
          <SystemBlueprintModal triggerClassName="blueprint-trigger blueprint-trigger--desk" />
          <button
            type="button"
            disabled={killing}
            onClick={onKill}
            className="cq-btn-danger"
          >
            {killing ? "KILLING…" : "EMERGENCY KILL"}
          </button>
        </div>
      </div>
      {killMsg ? (
        <div className="cq-mono text-[length:var(--desk-type-xs)] text-[var(--color-desk-bad)]">
          {killMsg}
        </div>
      ) : null}
    </div>
  );
}

export const DeskStatusBar = memo(DeskStatusBarImpl);
