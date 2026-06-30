import type { JsonObject, LogLine } from "../types/cockpit";
import type { CandlePoint, PricePoint, SignalMarker } from "../types/cockpit";
import { logFromTick } from "./logStream";
import {
  appendPnLPoint,
  buildSignalMarkers,
  tickToCandle,
} from "./mappers";

export interface TickAccumulator {
  candleHistory: CandlePoint[];
  pnlHistory: PricePoint[];
  markers: SignalMarker[];
}

/** Process a burst of ticks in one pass — single React update. */
export function processTickBatch(
  ticks: JsonObject[],
  acc: TickAccumulator,
  prevTick: JsonObject | null,
  pnl: JsonObject | null,
): { acc: TickAccumulator; prevTick: JsonObject | null; logs: LogLine[] } {
  let prev = prevTick;
  const logs: LogLine[] = [];
  let candles = acc.candleHistory;
  let pnlHist = acc.pnlHistory;
  let marks = acc.markers;

  for (const tick of ticks) {
    candles = tickToCandle(tick, candles);
    pnlHist = appendPnLPoint(pnlHist, tick, pnl);
    marks = buildSignalMarkers(tick, prev, marks);
    logs.push(...logFromTick(tick, prev));
    prev = tick;
  }

  return {
    acc: { candleHistory: candles, pnlHistory: pnlHist, markers: marks },
    prevTick: prev,
    logs,
  };
}

/** RAF-batched tick queue — coalesces bursts into one flush per frame. */
export class TickBatcher {
  private queue: JsonObject[] = [];
  private rafId: number | null = null;

  constructor(private readonly flush: (ticks: JsonObject[]) => void) {}

  push(tick: JsonObject): void {
    this.queue.push(tick);
    if (this.rafId !== null) return;
    this.rafId = requestAnimationFrame(() => {
      this.rafId = null;
      const batch = this.queue;
      this.queue = [];
      if (batch.length) this.flush(batch);
    });
  }

  dispose(): void {
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.queue = [];
  }
}
