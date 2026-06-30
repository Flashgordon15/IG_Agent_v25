/**
 * IG Cockpit runtime validation suite — simulates live tick load and coherency.
 * Run: npm run test:runtime
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { debounce, mergeIfNewer, parsePayloadTs } from "./coherence";
import { mergeLogLines } from "./logStream";
import {
  isGuiFullyReady,
  mapRoutingPanel,
  mapStatusMatrix,
} from "./mappers";
import { RollingAverage } from "./metrics";
import { processTickBatch, TickBatcher } from "./tickProcessor";

function tick(i: number, mid = 2650 + i * 0.01): Record<string, unknown> {
  const ts = new Date(Date.UTC(2026, 5, 29, 12, 0, i % 60)).toISOString();
  return {
    ts,
    bid: mid - 0.5,
    offer: mid + 0.5,
    daily_pnl_gbp: i * 0.1,
    stream_status: "LIVE",
    signal: { direction: i % 40 === 0 ? "BUY" : "WAIT", confidence: 72 },
  };
}

describe("1. WebSocket / TickBatcher", () => {
  let rafCb: FrameRequestCallback | null = null;

  beforeEach(() => {
    rafCb = null;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      rafCb = cb;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {
      rafCb = null;
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("coalesces 200 tick pushes into one flush per frame", () => {
    const flushes: number[] = [];
    const batcher = new TickBatcher((batch) => flushes.push(batch.length));

    for (let i = 0; i < 200; i += 1) {
      batcher.push(tick(i));
    }

    expect(flushes).toHaveLength(0);
    expect(rafCb).not.toBeNull();
    rafCb!(performance.now());
    expect(flushes).toHaveLength(1);
    expect(flushes[0]).toBe(200);
    batcher.dispose();
  });

  it("processes 200-tick burst in single batch without throwing", () => {
    const ticks = Array.from({ length: 200 }, (_, i) => tick(i));
    const result = processTickBatch(
      ticks,
      { candleHistory: [], pnlHistory: [], markers: [] },
      null,
      null,
    );
    expect(result.acc.candleHistory.length).toBeGreaterThan(0);
    expect(result.acc.pnlHistory.length).toBeGreaterThan(0);
    expect(result.prevTick).toBe(ticks[199]);
  });
});

describe("2. REST/WS coherency", () => {
  it("mergeIfNewer rejects REST older than WS tick", () => {
    const wsTs = Date.parse("2026-06-29T12:00:30.000Z");
    const stale = { ts: "2026-06-29T12:00:10.000Z", daily_pnl_gbp: 1 };
    const fresh = { ts: "2026-06-29T12:00:25.000Z", daily_pnl_gbp: 5 };
    const kept = mergeIfNewer(fresh, stale, wsTs);
    expect(kept.daily_pnl_gbp).toBe(5);
  });

  it("mergeIfNewer accepts newer REST when WS is idle", () => {
    const older = { ts: "2026-06-29T12:00:10.000Z", v: 1 };
    const newer = { ts: "2026-06-29T12:00:20.000Z", v: 2 };
    expect(mergeIfNewer(older, newer, 0).v).toBe(2);
  });

  it("out-of-order REST keeps newer current", () => {
    const current = { ts: "2026-06-29T12:00:30.000Z", v: 3 };
    const older = { ts: "2026-06-29T12:00:15.000Z", v: 1 };
    expect(mergeIfNewer(current, older, 0).v).toBe(3);
  });

  it("debounce collapses rapid calls to one after 350ms", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const d = debounce(fn, 350);
    d();
    d();
    d();
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(349);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});

describe("3. Chart data stability", () => {
  it("incremental candle updates same bucket without length explosion", () => {
    const ticks = Array.from({ length: 50 }, (_, i) => tick(i, 2650));
    let acc = {
      candleHistory: [] as ReturnType<typeof processTickBatch>["acc"]["candleHistory"],
      pnlHistory: [] as ReturnType<typeof processTickBatch>["acc"]["pnlHistory"],
      markers: [] as ReturnType<typeof processTickBatch>["acc"]["markers"],
    };
    for (const t of ticks) {
      const r = processTickBatch([t], acc, null, null);
      acc = r.acc;
    }
    expect(acc.candleHistory.length).toBeLessThanOrEqual(50);
    expect(acc.candleHistory.length).toBeGreaterThan(0);
  });
});

describe("4. StatusMatrix", () => {
  it("routing warm-up progress 0→100 maps correctly", () => {
    const gui0 = {
      session_status: "HEALTHY",
      session_id: "abc",
      gui_attach_ready: true,
      unified_execution_route: [],
      api_feed_health: [{ epic: "E1", fresh: false }],
      trade_pipeline_health: [],
    };
    const rows0 = mapStatusMatrix(gui0, null, new Date().toISOString());
    const routing0 = rows0.find((r) => r.key === "routing");
    expect(routing0?.progress).toBe(0);

    const guiFull = {
      ...gui0,
      unified_execution_route: [
        { epic: "E1", execution_path: "PATH_A" },
        { epic: "E2", execution_path: "PATH_A" },
        { epic: "E3", execution_path: "PATH_A" },
        { epic: "E4", execution_path: "PATH_A" },
      ],
    };
    const rowsFull = mapStatusMatrix(guiFull, null, new Date().toISOString());
    expect(rowsFull.find((r) => r.key === "routing")?.progress).toBe(100);
  });

  it("degraded feeds when partial fresh", () => {
    const gui = {
      session_status: "HEALTHY",
      session_id: "x",
      gui_attach_ready: true,
      unified_execution_route: [{ epic: "E1", execution_path: "PATH_A" }],
      api_feed_health: [
        { epic: "E1", fresh: true },
        { epic: "E2", fresh: false },
      ],
      trade_pipeline_health: [],
    };
    const rows = mapStatusMatrix(gui, { stream_status: "LIVE" }, null);
    expect(rows.find((r) => r.key === "feeds")?.status).toBe("warn");
  });
});

describe("5. Routing panel metrics", () => {
  it("rolling average smooths latency spikes", () => {
    const avg = new RollingAverage(8);
    const samples = [50, 52, 48, 200, 51, 49, 50, 52];
    let last = 0;
    for (const s of samples) last = avg.push(s);
    expect(last).toBeLessThan(100);
    expect(last).toBeGreaterThan(48);
  });

  it("mapRoutingPanel includes slippage trend field", () => {
    const routing = {
      unified_execution_route: [
        {
          epic: "CS.D.CFPGOLD.CFP.IP",
          execution_path: "PATH_A",
          route_confidence: 80,
          route_reason: "ok",
          route_flags: [],
        },
      ],
    };
    const gui = {
      api_feed_health: [
        { epic: "CS.D.CFPGOLD.CFP.IP", fresh: true, age_s: 0.05, spread: 0.3 },
      ],
      trade_pipeline_health: [
        {
          epic: "CS.D.CFPGOLD.CFP.IP",
          fill_price: 2650,
          last_price: 2649,
          order_confirmed: true,
        },
      ],
    };
    const rows = mapRoutingPanel(routing, gui);
    expect(rows[0].latencyMs).not.toBeNull();
    expect(["up", "down", "flat"]).toContain(rows[0].slippageTrend);
  });
});

describe("6. GUI readiness", () => {
  it("isGuiFullyReady requires all critical fields", () => {
    const partial = {
      session_status: "HEALTHY",
      session_id: "1",
      gui_attach_ready: true,
    };
    expect(isGuiFullyReady(partial)).toBe(false);
    const full = {
      ...partial,
      strategy_selector_advice: [],
      strategy_controller_decisions: [],
      strategy_governance: [],
      unified_execution_route: [],
      hard_enforcement_decisions: [],
      trade_pipeline_health: [],
      regime_detection: [],
      regime_risk_envelope: [],
      daily_pnl_targeting: {},
    };
    expect(isGuiFullyReady(full)).toBe(true);
  });
});

describe("7. Logs throughput", () => {
  it("mergeLogLines caps volume and batches", () => {
    const existing = Array.from({ length: 490 }, (_, i) => ({
      id: `e-${i}`,
      ts: String(i),
      message: "m",
      level: "info" as const,
      subsystem: "execution" as const,
    }));
    const incoming = Array.from({ length: 50 }, (_, i) => ({
      id: `n-${i}`,
      ts: String(500 + i),
      message: "new",
      level: "info" as const,
      subsystem: "feeds" as const,
    }));
    const merged = mergeLogLines(existing, incoming, 500);
    expect(merged.length).toBeLessThanOrEqual(500);
    expect(merged.length).toBe(500);
  });
});

describe("8. Timestamp parsing", () => {
  it("parsePayloadTs handles ISO strings", () => {
    expect(parsePayloadTs({ ts: "2026-06-29T12:00:00.000Z" })).toBeGreaterThan(0);
    expect(parsePayloadTs(null)).toBe(0);
  });
});

describe("9. Safety — fire-and-forget agent control", () => {
  it("pause/resume/suppress return void (non-blocking)", async () => {
    const { pauseTrading, resumeTrading, suppressOrders } = await import("./api");
    expect(pauseTrading()).toBeUndefined();
    expect(resumeTrading()).toBeUndefined();
    expect(suppressOrders()).toBeUndefined();
  });
});
