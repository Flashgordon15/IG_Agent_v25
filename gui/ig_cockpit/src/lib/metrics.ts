/** Rolling average with O(1) updates for latency smoothing. */
export class RollingAverage {
  private readonly window: number[] = [];

  constructor(private readonly size: number) {}

  push(value: number): number {
    this.window.push(value);
    if (this.window.length > this.size) this.window.shift();
    return this.avg();
  }

  avg(): number {
    if (!this.window.length) return 0;
    return this.window.reduce((a, b) => a + b, 0) / this.window.length;
  }
}

export class LatencyTracker {
  private readonly trackers = new Map<string, RollingAverage>();
  private readonly slippagePrev = new Map<string, number>();

  smoothLatency(epic: string, ms: number | null): number | null {
    if (ms === null) return null;
    let tracker = this.trackers.get(epic);
    if (!tracker) {
      tracker = new RollingAverage(8);
      this.trackers.set(epic, tracker);
    }
    return Math.round(tracker.push(ms));
  }

  slippageTrend(epic: string, bps: number | null): "up" | "down" | "flat" {
    if (bps === null) return "flat";
    const prev = this.slippagePrev.get(epic);
    this.slippagePrev.set(epic, bps);
    if (prev === undefined) return "flat";
    if (bps > prev + 0.5) return "up";
    if (bps < prev - 0.5) return "down";
    return "flat";
  }
}

export const latencyTracker = new LatencyTracker();

export function latencyColor(ms: number | null): "ok" | "warn" | "error" {
  if (ms === null) return "warn";
  if (ms < 120) return "ok";
  if (ms < 450) return "warn";
  return "error";
}

export function feedHealthLevel(
  fresh: number,
  total: number,
): "ok" | "warn" | "error" {
  if (total === 0) return "warn";
  const ratio = fresh / total;
  if (ratio >= 0.75) return "ok";
  if (ratio >= 0.4) return "warn";
  return "error";
}
