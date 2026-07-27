"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AIMarketScanner } from "@/components/gpu/AIMarketScanner";
import { DeskIntentStrip } from "@/components/gpu/DeskIntentStrip";
import { DualLaneControlDeck } from "@/components/gpu/DualLaneControlDeck";
import { ExecutionTruthStrip } from "@/components/gpu/ExecutionTruthStrip";
import { GPUExecutionCanvas } from "@/components/gpu/GPUExecutionCanvas";
import { HardwareAffinityMeters } from "@/components/gpu/HardwareAffinityMeters";
import { PerformanceMetricsBlotter } from "@/components/gpu/PerformanceMetricsBlotter";
import { RefPositionBlotter } from "@/components/gpu/RefPositionBlotter";
import { SovereignAccountingBoard } from "@/components/gpu/SovereignAccountingBoard";
import { GuiSupervisorChip } from "@/components/gpu/GuiSupervisorChip";
import { MlStrategyReviewChip } from "@/components/gpu/MlStrategyReviewChip";
import { SovereignSystemBadge } from "@/components/gpu/SovereignSystemBadge";
import { TacticalMacroObiStrip } from "@/components/gpu/TacticalMacroObiStrip";
import { TokenBucketProgressBars } from "@/components/gpu/TokenBucketProgressBars";
import { useDeskCapital } from "@/hooks/useDeskCapital";
import { useGpuExecutionStream } from "@/hooks/useGpuExecutionStream";
import { useQuantumNodeMemory } from "@/hooks/useQuantumNodeMemory";
import { fetchAgentJson } from "@/lib/agent-client";

type HealthState = {
  is_healthy?: boolean;
  quote_age_ms?: number | null;
  quote_age_sec?: number | null;
  operational_badge?: boolean;
  trading_path_live?: boolean;
  trading_path_badge?: string;
  trading_path_primary?: { code?: string; label?: string } | null;
  last_reason?: string;
  desk_rag?: string;
  desk_rag_label?: string;
};

type OpsHonesty = {
  desk_stability?: {
    grade?: string;
    label?: string;
    boot_gate?: {
      boot_started_at?: number;
      boot_latency_buffer_sec?: number;
      boot_latency_buffer_active?: boolean;
    };
  };
  boot_gate?: {
    boot_started_at?: number;
    boot_latency_buffer_sec?: number;
    boot_latency_buffer_active?: boolean;
  };
  broker_open_sot?: { count?: number | null; ok?: boolean };
  desk_liveness?: { ok?: boolean | null };
  feed_transport_summary?: { label?: string; streaming_transport?: string };
  desk_rag?: string;
  desk_rag_label?: string;
  trading_path_live?: boolean;
  trading_path_badge?: string;
  healing_active?: boolean;
  dual_engine_operational?: boolean;
  orchestrator?: { healing_active?: boolean; dual_engine_operational?: boolean };
  core_detached?: boolean;
  maintenance_detached_badge?: string | null;
  grok_macro_bias?: string;
};

export function GpuPlatformShell() {
  const { mounted, view } = useQuantumNodeMemory();
  const { bufferRef, chrome } = useGpuExecutionStream();
  const capital = useDeskCapital(
    view?.totalUplGbp ?? chrome.sessionUnrealizedGbp ?? null,
    view?.positions ?? [],
  );
  const [health, setHealth] = useState<HealthState>({});
  const [ops, setOps] = useState<OpsHonesty>({});
  const onHealth = useCallback((state: HealthState | undefined) => {
    if (state) setHealth(state);
  }, []);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const strip = await fetchAgentJson<OpsHonesty>(
          "/api/desk/ops_strip",
          undefined,
          2500,
        );
        if (alive && strip) setOps(strip);
      } catch {
        /* keep last */
      }
    };
    void pull();
    const id = window.setInterval(pull, 8000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const quoteAgeMs =
    health.quote_age_ms ??
    (health.quote_age_sec != null
      ? Number(health.quote_age_sec) * 1000
      : chrome.quoteAgeMs);

  const feedTransport =
    bufferRef.current.feedTransport ||
    ops.feed_transport_summary?.streaming_transport ||
    "rest_poll";

  const macroBias =
    ops.grok_macro_bias || view?.safety?.macroBias || "NEUTRAL";

  if (!mounted || !view) {
    return (
      <div className="gpu-platform gpu-platform--boot">
        <div className="gpu-boot-frame">
          <p className="gpu-kicker">Command Deck</p>
          <h1 className="gpu-safety-title">Aligning sovereign desk multiplex…</h1>
          <p className="gpu-boot-sub">
            feed health · simplified IG ledger · open-book blotter
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="gpu-platform sniper-deck hybrid-desk sovereign-desk v36-tactical-grid">
      <SovereignSystemBadge
        isHealthy={health.is_healthy !== false}
        quoteAgeMs={quoteAgeMs ?? null}
        operational={Boolean(health.operational_badge)}
        tradingPathLive={
          ops.trading_path_live ?? health.trading_path_live
        }
        tradingPathBadge={
          ops.trading_path_badge || health.trading_path_badge
        }
        tradingPathReason={
          health.trading_path_primary?.label || health.last_reason || null
        }
        deskRag={ops.desk_rag || health.desk_rag}
        deskRagLabel={ops.desk_rag_label || health.desk_rag_label}
        stabilityGrade={ops.desk_stability?.grade || ops.desk_rag}
        sotOk={ops.broker_open_sot?.ok}
        sotCount={
          ops.broker_open_sot?.count != null
            ? Number(ops.broker_open_sot.count)
            : chrome.openCount
        }
        livenessOk={ops.desk_liveness?.ok}
        bookFlat={
          (ops.broker_open_sot?.count != null
            ? Number(ops.broker_open_sot.count)
            : chrome.openCount) === 0 &&
          ops.broker_open_sot?.ok !== false &&
          ops.desk_liveness?.ok !== false
        }
        feedLabel={ops.feed_transport_summary?.label || null}
        feedTransport={feedTransport}
        dualEngineMode
        dualEngineOperational={chrome.dualPortOperational}
        cfdPortOnline={chrome.cfdOnline}
        sbPortOnline={chrome.sbOnline}
        feedBlockageMs={chrome.feedBlockageMs}
        bootStartedAtSec={
          ops.boot_gate?.boot_started_at ??
          ops.desk_stability?.boot_gate?.boot_started_at ??
          null
        }
        bootLatencyBufferSec={
          ops.boot_gate?.boot_latency_buffer_sec ??
          ops.desk_stability?.boot_gate?.boot_latency_buffer_sec ??
          30
        }
        healingActive={
          Boolean(
            ops.healing_active ??
              ops.orchestrator?.healing_active,
          )
        }
        coreDetached={Boolean(ops.core_detached)}
      />
      <GuiSupervisorChip />
      <MlStrategyReviewChip />

      <DeskIntentStrip
        bufferRef={bufferRef}
        cfdOnline={chrome.cfdOnline}
        sbOnline={chrome.sbOnline}
      />

      <DualLaneControlDeck
        bufferRef={bufferRef}
        structureRevision={chrome.structureRevision}
        coreDetached={Boolean(ops.core_detached)}
      />

      <SovereignAccountingBoard onHealth={onHealth} />

      <div className="v36-asymmetric-grid">
        <main className="v36-tactical-radar" aria-label="Tactical radar">
          <GPUExecutionCanvas
            bufferRef={bufferRef}
            chrome={chrome}
            milestoneGbp={capital.milestoneGbp}
            progressPct={capital.progressPct}
            provisionalCashGbp={capital.provisionalCashGbp}
          />
          <TacticalMacroObiStrip
            macroBias={macroBias}
            lastObi={view.alpha?.lastObi ?? 0}
            focusLabel={view.alpha?.label ?? "—"}
          />
          <AIMarketScanner
            rows={view.scanner ?? []}
            wsState={chrome.wsState === "live" ? "live" : view.wsState}
            capital={capital}
            muxOpenCount={chrome.openCount}
            deskIdleReason={view.deskIdleReason}
            rankedChrome={view.rankedChrome ?? null}
          />
        </main>

        <aside className="v36-command-strip" aria-label="System command strip">
          <HardwareAffinityMeters />
          <TokenBucketProgressBars />
          <ExecutionTruthStrip bufferRef={bufferRef} chrome={chrome} />
          <PerformanceMetricsBlotter />
          <RefPositionBlotter
            bufferRef={bufferRef}
            structureRevision={chrome.structureRevision}
            openCount={chrome.openCount}
          />
        </aside>
      </div>

      <footer className="gpu-platform-foot">
        <span className="gpu-foot-mono">
          V36 ASYMMETRIC GRID · MUX {chrome.muxSource.toUpperCase()} · OPEN{" "}
          {chrome.openCount} · SETTLED £{capital.realizedTodayGbp.toFixed(2)}
        </span>
        <Link href="/classic" className="gpu-foot-link">
          → Adaptive Logistics (ops)
        </Link>
      </footer>
    </div>
  );
}
