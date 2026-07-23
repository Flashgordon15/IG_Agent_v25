"use client";

import { useState } from "react";
import { SystemBlueprintModal } from "@/components/SystemBlueprintModal";
import type { QuantumSafetyMatrix } from "@/lib/quantum-node-types";
import { agentHttpBase } from "@/lib/agent-client";

type Props = {
  safety: QuantumSafetyMatrix | null;
  onEmergency: () => void;
};

function tone(status: string): "ok" | "warn" | "bad" {
  if (status === "OK" || status === "READY" || status === "SYNCED") return "ok";
  if (
    status === "DEGRADED" ||
    status === "COMPILING" ||
    status === "LAGGING" ||
    status === "VETO"
  )
    return "warn";
  return "bad";
}

export function HardwareSafetyBanner({ safety, onEmergency }: Props) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  const cells = [
    {
      key: "FEED / DRIVER",
      value: safety?.feedLabel ?? safety?.driverIntegrity ?? "—",
      tone: tone(safety?.driverIntegrity ?? ""),
    },
    {
      key: "MEMORY CTX",
      value: safety?.memoryAlignment ?? safety?.cudaCompiler ?? "—",
      tone: tone(safety?.cudaCompiler ?? ""),
    },
    {
      key: "POS / DB SYNC",
      value: safety?.databaseSync ?? "—",
      tone: tone(safety?.databaseSync ?? ""),
    },
    {
      key: "MACRO BIAS",
      value: safety?.macroBias ?? "—",
      tone: tone(safety?.macroBias === "VETO" ? "VETO" : safety?.macroBias ?? ""),
    },
  ];

  async function handleKill() {
    if (!armed) {
      setArmed(true);
      window.setTimeout(() => setArmed(false), 4000);
      return;
    }
    setBusy(true);
    try {
      onEmergency();
      await fetch(`${agentHttpBase()}/api/v1/emergency/kill`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          source: "quantum_node_banner",
          purge_queues: true,
        }),
      }).catch(() => null);
    } finally {
      setBusy(false);
      setArmed(false);
    }
  }

  return (
    <div className="gpu-safety-banner" role="region" aria-label="Hardware protection">
      <div className="gpu-safety-brand">
        <span className="gpu-safety-mark" aria-hidden />
        <div>
          <p className="gpu-kicker">Hardware Protection · Quantum Node</p>
          <h1 className="gpu-safety-title">4K Hybrid Multi-Asset Desk</h1>
        </div>
      </div>

      <div className="gpu-safety-matrix" role="status">
        {cells.map((c) => (
          <div key={c.key} className={`gpu-safety-cell gpu-safety-cell--${c.tone}`}>
            <span className="gpu-metric-key">{c.key}</span>
            <strong>{c.value}</strong>
          </div>
        ))}
      </div>

      <div className="gpu-safety-actions">
        <SystemBlueprintModal triggerClassName="blueprint-trigger blueprint-trigger--banner" />
        <button
          type="button"
          className={`gpu-kill-btn${armed ? " gpu-kill-btn--armed" : ""}`}
          disabled={busy}
          onClick={handleKill}
        >
          {busy
            ? "PURGING…"
            : armed
              ? "CONFIRM — PURGE ALL QUEUES"
              : "EMERGENCY SHUTDOWN / PURGE ALL QUEUES"}
        </button>
      </div>
    </div>
  );
}
