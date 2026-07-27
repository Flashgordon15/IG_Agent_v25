"use client";

/**
 * Multiplexed desk stream — dual REST lanes (CFD :8080 · SB :8081) + CFD WS ticks.
 * REST bootstrap/recovery on both ports; positions bound per-engine via ref matrix.
 */

import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { cfdHttpBase, deskWsBase, sbHttpBase } from "@/lib/desk-api-bases";
import {
  bootstrapDualPorts,
  type PortDeskSnapshot,
} from "@/lib/desk-port-bootstrap";
import {
  createGpuExecutionBuffer,
  gbpToPriceLevel,
  positionsFingerprint,
  pushCrawl,
  pushMid,
  type GpuExecutionBuffer,
} from "@/lib/gpu-execution-buffer";
import {
  buildDualPortLanes,
  laneOperational,
  mergeDualPortEnvelope,
  normalizeDeskMultiplex,
  type DeskMultiplexEnvelope,
  type SniperArmState,
} from "@/lib/desk-multiplex";

const DOW = "IX.D.DOW.IFM.IP";
const CHROME_HZ = 2;
const WS_STALE_RECOVERY_MS = 8000;
const RECOVERY_MIN_GAP_MS = 10000;
/** REST-friendly dual-port poll — aligns with backend accounting cache (~15–20s). */
const DUAL_PORT_POLL_MS = 12_000;

export type GpuExecutionChrome = {
  wsState: "connecting" | "live" | "offline";
  openCount: number;
  focusLabel: string;
  focusEpic: string;
  lastMid: number;
  revision: number;
  structureRevision: number;
  quoteAgeMs: number | null;
  sniperArm: SniperArmState;
  gateVerdict: string;
  sessionRealizedGbp: number;
  sessionUnrealizedGbp: number;
  muxSource: GpuExecutionBuffer["lastMuxSource"];
  dualPortOperational: boolean;
  cfdOnline: boolean;
  sbOnline: boolean;
  /** Ms since last WS/bootstrap frame — TELEMETRY LOSS badge at >5s. */
  feedBlockageMs: number | null;
};

function syncFocusAndCrawl(buf: GpuExecutionBuffer): void {
  const focus =
    buf.positions.find((p) => p.epic === buf.focusEpic) ?? buf.positions[0];
  if (focus) {
    buf.focusEpic = focus.epic || DOW;
    buf.focusLabel = focus.label || "OPEN";
  } else if (!buf.positions.length) {
    buf.focusEpic = DOW;
    buf.focusLabel = "WALL ST / DOW";
  }
  if (!focus || !(focus.entry > 0)) return;
  const softGbp =
    focus.softLossGbp != null && focus.softLossGbp > 0
      ? Math.abs(focus.softLossGbp)
      : null;
  const trailGbp =
    focus.trailFloorGbp != null && focus.trailFloorGbp > 0
      ? focus.trailFloorGbp
      : null;
  pushCrawl(
    buf.softCrawl,
    softGbp
      ? gbpToPriceLevel(
          focus.entry,
          focus.direction,
          softGbp,
          focus.size,
          "stop",
          focus.epic,
        )
      : null,
  );
  pushCrawl(
    buf.trailCrawl,
    trailGbp
      ? gbpToPriceLevel(
          focus.entry,
          focus.direction,
          trailGbp,
          focus.size,
          "profit",
          focus.epic,
        )
      : null,
  );
}

function applyPortHealth(buf: GpuExecutionBuffer, cfd: PortDeskSnapshot, sb: PortDeskSnapshot): void {
  buf.portHealth = {
    cfd: {
      online: cfd.online,
      healthOk: cfd.healthOk,
      quoteAgeMs: cfd.health.quoteAgeMs,
    },
    sb: {
      online: sb.online,
      healthOk: sb.healthOk,
      quoteAgeMs: sb.health.quoteAgeMs,
    },
  };
  buf.engines = buildDualPortLanes(
    {
      online: cfd.online,
      healthOk: cfd.healthOk,
      envelope: cfd.envelope,
      transport: cfd.health.transport,
    },
    {
      online: sb.online,
      healthOk: sb.healthOk,
      envelope: sb.envelope,
      transport: sb.health.transport,
    },
  );
}

function applyEnvelope(buf: GpuExecutionBuffer, env: DeskMultiplexEnvelope): void {
  const prevFp = positionsFingerprint(buf.positions);
  if (env.positions.length > 0 || env.source !== "ws") {
    buf.positions = env.positions;
  }
  const nextFp = positionsFingerprint(buf.positions);
  if (nextFp !== prevFp) buf.structureRevision += 1;

  const focusTick =
    env.ticks.find((t) => t.epic === buf.focusEpic) ||
    env.ticks.find((t) => t.epic === DOW) ||
    env.ticks[0];
  if (focusTick) pushMid(buf, focusTick.mid);

  buf.arms = env.arms;
  buf.sessionPnl = env.session_pnl;
  buf.truth = env.truth;
  buf.lastMuxSource = env.source;
  if (env.engines?.length) buf.engines = env.engines;
  if (env.feedTransport) buf.feedTransport = env.feedTransport;
  if (env.source === "ws") buf.wsLive = true;
  syncFocusAndCrawl(buf);
  buf.revision += 1;
  buf.updatedAt = performance.now();
}

function applyDualPortSnapshots(
  buf: GpuExecutionBuffer,
  cfd: PortDeskSnapshot,
  sb: PortDeskSnapshot,
  source: DeskMultiplexEnvelope["source"],
): void {
  applyPortHealth(buf, cfd, sb);
  const merged = mergeDualPortEnvelope(cfd.envelope, sb.envelope, source);
  merged.engines = buf.engines;
  applyEnvelope(buf, merged);

  const brokerOpenSot =
    (cfd.brokerOpenSot ?? 0) + (sb.brokerOpenSot ?? 0);
  if (Number.isFinite(brokerOpenSot)) {
    buf.brokerOpenSotCount = brokerOpenSot;
  }
}

function applyWsCfdFrame(buf: GpuExecutionBuffer, raw: unknown): void {
  const cfdEnv = normalizeDeskMultiplex(raw, "ws");
  const sbLane = buf.engines.find((l) => l.engineId === "sb_sentinel");
  const cfdLane = buf.engines.find((l) => l.engineId === "cfd_sniper");
  const cfdPositions =
    cfdEnv.positions.length > 0 ? cfdEnv.positions : (cfdLane?.positions ?? []);
  const sbPositions = sbLane?.positions ?? [];

  if (cfdLane) {
    cfdLane.quoteAgeMs = cfdEnv.truth.quoteAgeMs;
    cfdLane.positions = cfdPositions;
    cfdLane.openCount = cfdPositions.length;
    cfdLane.operational = laneOperational(cfdLane);
  }

  applyEnvelope(buf, {
    ...cfdEnv,
    positions: [...cfdPositions, ...sbPositions],
    engines: buf.engines,
  });
}

export function useGpuExecutionStream(): {
  bufferRef: MutableRefObject<GpuExecutionBuffer>;
  chrome: GpuExecutionChrome;
} {
  const bufferRef = useRef<GpuExecutionBuffer>(createGpuExecutionBuffer());
  const [chrome, setChrome] = useState<GpuExecutionChrome>({
    wsState: "connecting",
    openCount: 0,
    focusLabel: "WALL ST / DOW",
    focusEpic: DOW,
    lastMid: 0,
    revision: 0,
    structureRevision: 0,
    quoteAgeMs: null,
    sniperArm: "SUPPRESSED",
    gateVerdict: "GATE_STANDBY_WAITING_BREAKOUT",
    sessionRealizedGbp: 0,
    sessionUnrealizedGbp: 0,
    muxSource: "idle",
    dualPortOperational: false,
    cfdOnline: false,
    sbOnline: false,
    feedBlockageMs: null,
  });
  const wsStateRef = useRef<GpuExecutionChrome["wsState"]>("connecting");
  const lastFrameAtRef = useRef(0);
  const lastRecoveryAtRef = useRef(0);
  const cfdBase = cfdHttpBase();
  const sbBase = sbHttpBase();

  useEffect(() => {
    let cancelled = false;

    const run = async (source: "bootstrap" | "recovery") => {
      try {
        const { cfd, sb } = await bootstrapDualPorts(cfdBase, sbBase, source);
        if (cancelled) return;
        applyDualPortSnapshots(bufferRef.current, cfd, sb, source);
        lastRecoveryAtRef.current = Date.now();
      } catch {
        /* keep last buffer */
      }
    };

    void run("bootstrap");

    const id = window.setInterval(() => {
      const buf = bufferRef.current;
      const silentFor = Date.now() - lastFrameAtRef.current;
      const sinceRecovery = Date.now() - lastRecoveryAtRef.current;
      const midBroken =
        !(buf.lastMid >= 1000) &&
        (buf.focusEpic.includes("DOW") ||
          buf.focusEpic.includes("DAX") ||
          buf.focusEpic.includes("NIKKEI"));
      if (
        sinceRecovery > RECOVERY_MIN_GAP_MS &&
        (lastFrameAtRef.current === 0 ||
          silentFor > WS_STALE_RECOVERY_MS ||
          midBroken ||
          !buf.portHealth.sb.online ||
          !buf.portHealth.cfd.online)
      ) {
        void run("recovery");
      }
    }, DUAL_PORT_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [cfdBase, sbBase]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;
    let attempt = 0;

    const connect = () => {
      if (cancelled) return;
      try {
        ws = new WebSocket(`${deskWsBase("cfd")}/ws/stream`);
      } catch {
        wsStateRef.current = "offline";
        bufferRef.current.wsLive = false;
        timer = setTimeout(connect, 2000);
        return;
      }
      ws.onopen = () => {
        attempt = 0;
        wsStateRef.current = "live";
        bufferRef.current.wsLive = true;
      };
      ws.onmessage = (ev) => {
        try {
          const raw = JSON.parse(String(ev.data || "{}")) as unknown;
          applyWsCfdFrame(bufferRef.current, raw);
          lastFrameAtRef.current = Date.now();
          wsStateRef.current = "live";
          bufferRef.current.wsLive = true;
        } catch {
          /* ignore malformed */
        }
      };
      ws.onerror = () => {
        wsStateRef.current = "offline";
        bufferRef.current.wsLive = false;
      };
      ws.onclose = () => {
        if (cancelled) return;
        wsStateRef.current = "offline";
        bufferRef.current.wsLive = false;
        attempt += 1;
        timer = setTimeout(connect, Math.min(8000, 500 * 2 ** attempt));
      };
    };
    connect();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    };
  }, [cfdBase]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const buf = bufferRef.current;
      const lanesOk = buf.engines.length >= 2 && buf.engines.every((l) => l.operational);
      const portsOk =
        buf.portHealth.cfd.online &&
        buf.portHealth.sb.online &&
        buf.portHealth.cfd.healthOk &&
        buf.portHealth.sb.healthOk;
      setChrome((prev) => {
        const lastAt = lastFrameAtRef.current;
        const feedBlockageMs =
          lastAt > 0 ? Math.max(0, Date.now() - lastAt) : null;
        const next: GpuExecutionChrome = {
          wsState: wsStateRef.current,
          openCount: Math.max(
            buf.positions.length,
            buf.brokerOpenSotCount != null && Number.isFinite(buf.brokerOpenSotCount)
              ? buf.brokerOpenSotCount
              : buf.positions.length,
          ),
          focusLabel: buf.focusLabel,
          focusEpic: buf.focusEpic,
          lastMid: buf.lastMid,
          revision: buf.revision,
          structureRevision: buf.structureRevision,
          quoteAgeMs: buf.truth.quoteAgeMs,
          sniperArm: buf.truth.sniperArm,
          gateVerdict: buf.truth.gateVerdict,
          sessionRealizedGbp: buf.sessionPnl.realizedGbp,
          sessionUnrealizedGbp: buf.sessionPnl.unrealizedGbp,
          muxSource: buf.lastMuxSource,
          dualPortOperational: portsOk && lanesOk,
          cfdOnline: buf.portHealth.cfd.online,
          sbOnline: buf.portHealth.sb.online,
          feedBlockageMs,
        };
        if (
          prev.wsState === next.wsState &&
          prev.openCount === next.openCount &&
          prev.focusLabel === next.focusLabel &&
          prev.structureRevision === next.structureRevision &&
          prev.sniperArm === next.sniperArm &&
          prev.gateVerdict === next.gateVerdict &&
          prev.quoteAgeMs === next.quoteAgeMs &&
          prev.dualPortOperational === next.dualPortOperational &&
          prev.cfdOnline === next.cfdOnline &&
          prev.sbOnline === next.sbOnline &&
          prev.feedBlockageMs === next.feedBlockageMs &&
          Math.abs(prev.lastMid - next.lastMid) < 1e-9
        ) {
          return prev;
        }
        return next;
      });
    }, 1000 / CHROME_HZ);
    return () => window.clearInterval(id);
  }, []);

  return { bufferRef, chrome };
}
