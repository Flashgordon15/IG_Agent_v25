"use client";

import { useCallback, useState } from "react";
import { useAdaptiveLogistics } from "@/hooks/useAdaptiveLogistics";
import { regimeLabel } from "@/lib/adaptive-logistics-parse";
import { agentHttpBase, postAgentTune } from "@/lib/agent-client";

type Props = {
  focusEpic: string;
  fault?: boolean;
};

function Metric({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const toneClass =
    tone === "good"
      ? "text-[var(--color-desk-good)]"
      : tone === "warn"
        ? "text-[var(--color-desk-warn)]"
        : tone === "bad"
          ? "text-[var(--color-desk-bad)]"
          : "text-[var(--color-desk-text)]";
  return (
    <div className="min-w-0 flex-1 px-1">
      <div className="cq-label mb-1">{label}</div>
      <div className={`cq-display ${toneClass}`}>{value}</div>
      {hint ? (
        <div className="cq-mono mt-1 truncate text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
          {hint}
        </div>
      ) : null}
    </div>
  );
}

export function AdaptiveLogisticsPanel({ focusEpic, fault }: Props) {
  const { snapshot, error, refresh } = useAdaptiveLogistics(focusEpic);
  const [busy, setBusy] = useState(false);

  const regime = snapshot?.regime ?? "SPARSE";
  const gate = snapshot?.effectiveGateFloor ?? 55;
  const elastic = gate <= 38;
  const starvation = snapshot?.starvation;
  const autoDecay = starvation?.auto_decay_enabled ?? true;
  const mlFloor =
    starvation?.ml_veto_override ??
    starvation?.effective_ml_floor ??
    snapshot?.signalThreshold ??
    45;

  const toggleDecay = useCallback(async () => {
    setBusy(true);
    try {
      await postAgentTune({ auto_decay_enabled: !autoDecay });
      refresh?.();
    } finally {
      setBusy(false);
    }
  }, [autoDecay, refresh]);

  const live = !(fault || error);

  return (
    <section className="cq-panel overflow-hidden">
      <div
        className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-desk-line)]"
        style={{ padding: "0.65rem clamp(0.85rem, 1.2vw, 1.25rem)" }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-[length:clamp(0.95rem,0.85rem+0.35vw,1.2rem)] font-semibold tracking-tight">
            Adaptive Logistics
          </h1>
          <span className="cq-chip">
            <span className={live ? "cq-status-dot cq-status-dot-live" : "cq-status-dot cq-status-dot-warn"} />
            {live ? "LIVE" : error ? "DEGRADED" : "STALE"}
          </span>
          <span className="cq-chip text-[var(--color-desk-mute)]">
            {regimeLabel(regime)}
          </span>
        </div>
        <div className="cq-mono flex items-center gap-3 text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
          <span>{snapshot?.ticksCached?.toLocaleString() ?? "—"} ticks</span>
          <span className="hidden sm:inline">{agentHttpBase()}</span>
        </div>
      </div>

      <div
        className="grid items-stretch gap-3 sm:grid-cols-2 xl:grid-cols-5"
        style={{ padding: "0.85rem clamp(0.85rem, 1.2vw, 1.25rem)" }}
      >
        <Metric
          label="Regime"
          value={regimeLabel(regime).replace(" REGIME", "")}
          hint={(snapshot?.epicRows ?? [])
            .slice(0, 3)
            .map((r) => r.label)
            .join(" · ")}
          tone={regime.toLowerCase().includes("flat") ? "warn" : "default"}
        />
        <Metric
          label="Gate floor"
          value={`${gate.toFixed(0)}%`}
          hint={elastic ? "Elastic floor engaged" : "Baseline hold"}
          tone={elastic ? "good" : "default"}
        />
        <Metric
          label="Contract path"
          value={snapshot?.fractionalMicroLot ? "Micro" : "Integer"}
          hint={
            snapshot?.microLotSize != null
              ? `Last size ${snapshot.microLotSize}`
              : "Broker-compliant dispatch"
          }
          tone={snapshot?.fractionalMicroLot ? "good" : "default"}
        />
        <Metric
          label="ML veto floor"
          value={`${Number(mlFloor).toFixed(1)}%`}
          hint={`Decay ${starvation?.decay_steps ?? 0} · ${starvation?.state ?? "NORMAL"}`}
        />
        <div className="flex min-w-0 flex-col justify-between gap-2 px-1">
          <div className="cq-label">Fatigue</div>
          <button
            type="button"
            disabled={busy}
            onClick={() => void toggleDecay()}
            className="cq-chip w-fit transition hover:border-[rgba(61,220,151,0.35)]"
          >
            <span
              className={
                autoDecay
                  ? "cq-status-dot cq-status-dot-live"
                  : "cq-status-dot bg-[var(--color-desk-faint)]"
              }
            />
            Auto-decay {autoDecay ? "ON" : "OFF"}
          </button>
          <div className="cq-mono text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]">
            Focus {(focusEpic || "").split(".").slice(-2, -1)[0] || "—"}
          </div>
        </div>
      </div>

      {(snapshot?.brokerOverrides?.length ?? 0) > 0 ? (
        <div className="relative overflow-hidden border-t border-[var(--color-desk-line)] bg-black/20 py-1.5">
          <p className="cq-mono cq-marquee whitespace-nowrap px-3 text-[length:var(--desk-type-xs)] text-[var(--color-desk-warn)]">
            {(snapshot?.brokerOverrides ?? [])
              .map((l) => `[${l.epic.split(".").pop()}] ${l.raw}`)
              .join("   ◆   ")}
          </p>
        </div>
      ) : null}
    </section>
  );
}
