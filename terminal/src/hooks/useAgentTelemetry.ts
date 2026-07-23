"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { STREAM_STALE_MS } from "@/lib/constants";
import { agentWsBase, type TelemetryPayload } from "@/lib/agent-client";

export type WsState = "connecting" | "live" | "stale" | "offline";

const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];

/**
 * WebSocket connection state for DATA FEED indicator.
 * Payload is kept in a ref so high-frequency frames do not re-render the shell.
 */
export function useAgentTelemetry() {
  const [wsState, setWsState] = useState<WsState>("connecting");
  const payloadRef = useRef<TelemetryPayload | null>(null);
  const lastFrameRef = useRef(0);
  const attemptRef = useRef(0);

  const onFrame = useCallback((raw: TelemetryPayload) => {
    payloadRef.current = raw;
    lastFrameRef.current = Date.now();
    setWsState((prev) => (prev === "live" ? prev : "live"));
  }, []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let staleTimer: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      setWsState((prev) => (prev === "connecting" ? prev : "connecting"));
      const url = `${agentWsBase()}/api/telemetry/stream`;
      try {
        ws = new WebSocket(url);
      } catch {
        setWsState("offline");
        scheduleReconnect();
        return;
      }

      ws.onopen = () => {
        attemptRef.current = 0;
      };

      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(String(ev.data || "{}")) as TelemetryPayload;
          onFrame(data);
        } catch {
          /* malformed */
        }
      };

      ws.onerror = () => {
        if (!cancelled) setWsState("offline");
      };

      ws.onclose = () => {
        if (!cancelled) {
          setWsState("offline");
          scheduleReconnect();
        }
      };
    };

    const scheduleReconnect = () => {
      const delay =
        BACKOFF_MS[Math.min(attemptRef.current, BACKOFF_MS.length - 1)];
      attemptRef.current += 1;
      timer = setTimeout(connect, delay);
    };

    connect();

    staleTimer = setInterval(() => {
      const age = Date.now() - lastFrameRef.current;
      if (lastFrameRef.current > 0 && age > STREAM_STALE_MS) {
        setWsState((prev) => (prev === "stale" ? prev : "stale"));
      }
    }, 2000);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (staleTimer) clearInterval(staleTimer);
      if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    };
  }, [onFrame]);

  return { payloadRef, wsState };
}
