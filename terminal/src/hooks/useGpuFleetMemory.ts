"use client";

/**
 * Background GPU fleet memory tracker.
 * High-frequency samples live in refs / typed arrays; React state updates
 * are throttled (~4Hz) so canvas/SVG loops stay smooth at 4K.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  GpuFleetMemory,
  GpuSlotSnapshot,
  HardwareSafetyMatrix,
  TensorSeriesPoint,
} from "@/lib/gpu-fleet-types";

const HISTORY = 96;
const SERIES_CAP = 72;
const UI_HZ = 4;
const SAMPLE_MS = 50; // 20Hz background sample — not React

const FLEET_SEED: Array<{
  id: string;
  label: string;
  vramTotalGb: number;
  powerCap: number;
}> = [
  { id: "gpu0", label: "GPU 0 — RTX 5090", vramTotalGb: 32, powerCap: 575 },
  { id: "gpu1", label: "GPU 1 — H100 NVLink", vramTotalGb: 80, powerCap: 700 },
  { id: "gpu2", label: "GPU 2 — H100 NVLink", vramTotalGb: 80, powerCap: 700 },
  { id: "gpu3", label: "GPU 3 — RTX 5090", vramTotalGb: 32, powerCap: 575 },
];

function makeSlot(seed: (typeof FLEET_SEED)[number]): GpuSlotSnapshot {
  return {
    id: seed.id,
    label: seed.label,
    utilPct: 12 + Math.random() * 18,
    vramUsedGb: seed.vramTotalGb * (0.35 + Math.random() * 0.25),
    vramTotalGb: seed.vramTotalGb,
    powerW: seed.powerCap * (0.25 + Math.random() * 0.2),
    tempC: 42 + Math.random() * 12,
    utilHistory: new Float32Array(HISTORY),
    historyHead: 0,
    historyLen: 0,
  };
}

function pushUtil(slot: GpuSlotSnapshot, util01: number) {
  slot.utilHistory[slot.historyHead] = util01;
  slot.historyHead = (slot.historyHead + 1) % HISTORY;
  slot.historyLen = Math.min(HISTORY, slot.historyLen + 1);
}

function clonePublic(mem: GpuFleetMemory): GpuFleetMemory {
  return {
    tick: mem.tick,
    queuesPurgedAt: mem.queuesPurgedAt,
    safety: { ...mem.safety },
    tensor: {
      ...mem.tensor,
      series: mem.tensor.series.slice(-SERIES_CAP),
    },
    slots: mem.slots.map((s) => ({
      ...s,
      // Share typed array reference for canvas (read-only in UI)
      utilHistory: s.utilHistory,
    })),
  };
}

function bootstrapMemory(): GpuFleetMemory {
  const slots = FLEET_SEED.map(makeSlot);
  for (const s of slots) {
    for (let i = 0; i < 24; i++) pushUtil(s, s.utilPct / 100);
  }
  const series: TensorSeriesPoint[] = [];
  for (let i = 0; i < 24; i++) {
    series.push({
      step: i,
      tokensPerSec: 18000 + i * 40 + Math.random() * 400,
      loss: Math.max(0.12, 1.8 * Math.exp(-i / 18) + Math.random() * 0.04),
      convergence: Math.min(0.99, 1 - Math.exp(-i / 22)),
    });
  }
  return {
    tick: 0,
    queuesPurgedAt: null,
    slots,
    tensor: {
      step: 24,
      tokensTotal: 2_400_000,
      tokensPerSec: series[series.length - 1]?.tokensPerSec ?? 19000,
      batchSize: 128,
      loss: series[series.length - 1]?.loss ?? 0.4,
      convergence: series[series.length - 1]?.convergence ?? 0.6,
      series,
      seriesCap: SERIES_CAP,
    },
    safety: {
      driverIntegrity: "OK",
      cudaCompiler: "READY",
      databaseSync: "SYNCED",
      thermalTrip: false,
      lastUpdatedMs: Date.now(),
    },
  };
}

function stepMemory(mem: GpuFleetMemory, dtSec: number) {
  mem.tick += 1;
  let anyHot = false;

  for (let i = 0; i < mem.slots.length; i++) {
    const s = mem.slots[i];
    const seed = FLEET_SEED[i];
    // Smooth random walk — compute-looking load curves
    const target =
      0.35 +
      0.45 * (0.5 + 0.5 * Math.sin(mem.tick / (18 + i * 3))) +
      (Math.random() - 0.5) * 0.08;
    const util01 = Math.min(0.99, Math.max(0.04, s.utilPct / 100 + (target - s.utilPct / 100) * 0.12));
    s.utilPct = util01 * 100;
    pushUtil(s, util01);

    const vramTarget = seed.vramTotalGb * (0.42 + util01 * 0.45);
    s.vramUsedGb += (vramTarget - s.vramUsedGb) * 0.08;
    s.powerW += (seed.powerCap * (0.22 + util01 * 0.72) - s.powerW) * 0.1;
    s.tempC += (38 + util01 * 48 + (s.powerW / seed.powerCap) * 8 - s.tempC) * 0.06;
    if (s.tempC >= 82) anyHot = true;
  }

  const t = mem.tensor;
  t.step += 1;
  const conv = Math.min(0.995, 1 - Math.exp(-t.step / 140));
  t.convergence = conv;
  t.loss = Math.max(0.08, t.loss * (0.992 + Math.random() * 0.004));
  t.tokensPerSec = 16000 + conv * 12000 + Math.sin(t.step / 9) * 900 + Math.random() * 500;
  t.tokensTotal += Math.max(0, t.tokensPerSec * dtSec);
  t.batchSize = 128;
  t.series.push({
    step: t.step,
    tokensPerSec: t.tokensPerSec,
    loss: t.loss,
    convergence: t.convergence,
  });
  if (t.series.length > SERIES_CAP) t.series.splice(0, t.series.length - SERIES_CAP);

  const safety: HardwareSafetyMatrix = mem.safety;
  safety.lastUpdatedMs = Date.now();
  safety.thermalTrip = anyHot;
  if (anyHot && Math.random() < 0.015) safety.driverIntegrity = "DEGRADED";
  else if (!anyHot && Math.random() < 0.08) safety.driverIntegrity = "OK";
  else if (safety.driverIntegrity === "DEGRADED" && Math.random() < 0.01) {
    safety.driverIntegrity = "FAULT";
  }

  safety.cudaCompiler =
    t.step % 220 < 8
      ? "COMPILING"
      : safety.driverIntegrity === "FAULT"
        ? "ERROR"
        : safety.driverIntegrity === "OK"
          ? "READY"
          : "ERROR";
  safety.databaseSync =
    t.step % 180 < 5
      ? "LAGGING"
      : safety.driverIntegrity === "FAULT"
        ? "OFFLINE"
        : "SYNCED";
}

export function useGpuFleetMemory() {
  const memRef = useRef<GpuFleetMemory | null>(null);
  const [view, setView] = useState<GpuFleetMemory | null>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    memRef.current = bootstrapMemory();
    setView(clonePublic(memRef.current));

    let sampleTimer: number | undefined;
    let uiTimer: number | undefined;
    let last = performance.now();

    sampleTimer = window.setInterval(() => {
      const mem = memRef.current;
      if (!mem) return;
      const now = performance.now();
      const dt = Math.min(0.25, (now - last) / 1000);
      last = now;
      stepMemory(mem, dt);
    }, SAMPLE_MS);

    uiTimer = window.setInterval(() => {
      if (memRef.current) setView(clonePublic(memRef.current));
    }, 1000 / UI_HZ);

    return () => {
      if (sampleTimer) window.clearInterval(sampleTimer);
      if (uiTimer) window.clearInterval(uiTimer);
    };
  }, []);

  const purgeQueues = useCallback(() => {
    const mem = memRef.current;
    if (!mem) return;
    mem.queuesPurgedAt = Date.now();
    for (const s of mem.slots) {
      s.utilPct *= 0.15;
      s.powerW *= 0.2;
      s.tempC = Math.max(36, s.tempC - 18);
      for (let i = 0; i < 8; i++) pushUtil(s, s.utilPct / 100);
    }
    mem.tensor.tokensPerSec = 0;
    mem.safety.cudaCompiler = "READY";
    mem.safety.databaseSync = "SYNCED";
    mem.safety.thermalTrip = false;
    setView(clonePublic(mem));
  }, []);

  const getSlotHistory = useCallback((id: string) => {
    return memRef.current?.slots.find((s) => s.id === id) ?? null;
  }, []);

  return {
    mounted,
    fleet: view,
    memoryRef: memRef,
    purgeQueues,
    getSlotHistory,
  };
}
