/** Shared GPU fleet + tensor telemetry contracts (frontend memory plane). */

export type GpuSlotId = string;

export type GpuSlotSnapshot = {
  id: GpuSlotId;
  label: string; // e.g. "GPU 0 - RTX 5090"
  utilPct: number; // 0..100
  vramUsedGb: number;
  vramTotalGb: number;
  powerW: number;
  tempC: number;
  /** Rolling util samples for canvas sparkline (0..1) */
  utilHistory: Float32Array;
  historyHead: number;
  historyLen: number;
};

export type TensorSeriesPoint = {
  step: number;
  tokensPerSec: number;
  loss: number;
  convergence: number; // 0..1 approaching 1
};

export type HardwareSafetyMatrix = {
  driverIntegrity: "OK" | "DEGRADED" | "FAULT";
  cudaCompiler: "READY" | "COMPILING" | "ERROR";
  databaseSync: "SYNCED" | "LAGGING" | "OFFLINE";
  thermalTrip: boolean;
  lastUpdatedMs: number;
};

export type GpuFleetMemory = {
  slots: GpuSlotSnapshot[];
  tensor: {
    step: number;
    tokensTotal: number;
    tokensPerSec: number;
    batchSize: number;
    loss: number;
    convergence: number;
    /** Ring buffer of recent points for SVG charts */
    series: TensorSeriesPoint[];
    seriesCap: number;
  };
  safety: HardwareSafetyMatrix;
  queuesPurgedAt: number | null;
  tick: number;
};
