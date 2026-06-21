import { useCallback, useEffect, useRef, useState } from "react";
import {
  isApexDesktopShell,
  subscribeIpcStatus,
  subscribeTicks,
  getTransportLabel,
} from "../api/apexIpc.js";
import { API_BASE, DEFAULT_API_PORT, resolveTargetPort } from "../config.js";
import { IPC_STALE_MS } from "./constants.js";
import { parseApexTelemetry } from "./telemetryParser.js";

/**
 * Subscribe to Apex IPC tick stream with type-safe parsing and staleness detection.
 * @returns {{
 *   telemetry: import('./types.js').ParsedApexTelemetry | null,
 *   ipcConnected: boolean,
 *   networkDegraded: boolean,
 *   transport: string,
 *   tickCount: number,
 *   isDesktop: boolean,
 * }}
 */
export function useApexTelemetry() {
  const [telemetry, setTelemetry] = useState(null);
  const [ipcConnected, setIpcConnected] = useState(false);
  const [networkDegraded, setNetworkDegraded] = useState(false);
  const [tickCount, setTickCount] = useState(0);
  const lastTickAtRef = useRef(0);
  const isDesktop = isApexDesktopShell();

  const handleTick = useCallback((raw) => {
    const parsed = parseApexTelemetry(raw);
    if (!parsed) return;
    lastTickAtRef.current = Date.now();
    setTelemetry(parsed);
    setTickCount((n) => n + 1);
    setNetworkDegraded(false);
  }, []);

  useEffect(() => {
    let ws = null;
    let cancelled = false;
    const port = resolveTargetPort() || DEFAULT_API_PORT;
    const base =
      (typeof window !== "undefined" && window.apexConfig?.apiBase) ||
      API_BASE ||
      `http://127.0.0.1:${port}`;
    const wsUrl = `${base.replace(/^http/i, "ws").replace(/\/$/, "")}/api/telemetry/stream`;
    try {
      ws = new WebSocket(wsUrl);
      ws.onmessage = (event) => {
        if (cancelled) return;
        try {
          const payload = JSON.parse(String(event.data || "{}"));
          handleTick(payload);
          setIpcConnected(true);
          setNetworkDegraded(false);
        } catch {
          /* malformed frame */
        }
      };
      ws.onerror = () => {
        if (!cancelled) setNetworkDegraded(true);
      };
      ws.onclose = () => {
        if (!cancelled) setIpcConnected(false);
      };
    } catch {
      /* WebSocket unavailable in this shell */
    }
    return () => {
      cancelled = true;
      if (ws && ws.readyState <= WebSocket.OPEN) {
        ws.close();
      }
    };
  }, [handleTick]);

  useEffect(() => {
    if (window.apexIPC && typeof window.apexIPC.onTick === "function") {
      console.log("[APEX ENGINE] Native Zero-Copy IPC Preload Bridge engaged.");
      const unsubTick = window.apexIPC.onTick((tickPayload) => {
        handleTick(tickPayload);
      });
      const unsubStatus =
        typeof window.apexIPC.onStatus === "function"
          ? window.apexIPC.onStatus((status) => {
              const connected = Boolean(status?.connected);
              setIpcConnected(connected);
              if (!connected) {
                /* HTTP poll fallback — degraded only if poll also goes stale */
              }
            })
          : () => {};
      if (typeof window.apexIPC.getSidecarStatus === "function") {
        window.apexIPC.getSidecarStatus().then((status) => {
          const connected = Boolean(status?.ipcConnected || status?.apiHealthy);
          setIpcConnected(connected);
          if (connected) setNetworkDegraded(false);
        }).catch(() => {});
      }
      return () => {
        if (typeof unsubTick === "function") unsubTick();
        if (typeof unsubStatus === "function") unsubStatus();
      };
    }

    const unsubTick = subscribeTicks(handleTick);
    const unsubStatus = subscribeIpcStatus((status) => {
      const connected = Boolean(status?.connected);
      setIpcConnected(connected);
      if (!connected) {
        setNetworkDegraded(true);
      }
    });
    return () => {
      unsubTick();
      unsubStatus();
    };
  }, [handleTick]);

  useEffect(() => {
    if (!isDesktop) return undefined;
    let cancelled = false;
    const pollHttp = async () => {
      try {
        const port = resolveTargetPort() || DEFAULT_API_PORT;
        const base =
          (typeof window !== "undefined" && window.apexConfig?.apiBase) ||
          `http://127.0.0.1:${port}`;
        const res = await fetch(`${base.replace(/\/$/, "")}/state`, {
          headers: { "Content-Type": "application/json" },
        });
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (data && typeof data === "object") {
          handleTick(data);
          setIpcConnected(true);
          setNetworkDegraded(false);
        }
      } catch {
        /* sidecar may be restarting */
      }
    };
    pollHttp();
    const id = window.setInterval(pollHttp, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [isDesktop, handleTick]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const last = lastTickAtRef.current;
      if (last === 0) return;
      const stale = Date.now() - last > IPC_STALE_MS;
      if (stale && isDesktop) {
        // Only flag degraded when both IPC and HTTP poll have gone stale
        setNetworkDegraded(true);
        setIpcConnected(false);
      }
    }, 1000);
    return () => window.clearInterval(id);
  }, [isDesktop]);

  return {
    telemetry,
    ipcConnected,
    networkDegraded,
    transport: getTransportLabel(),
    tickCount,
    isDesktop,
  };
}
