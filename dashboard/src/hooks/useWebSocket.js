import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

const WS_URL =
  (typeof window !== "undefined" &&
    `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws`) ||
  "ws://127.0.0.1:8080/ws";

export function useWebSocket() {
  const [tick, setTick] = useState(null);
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const wsRef = useRef(null);
  const backoffRef = useRef(1000);
  const mountedRef = useRef(true);

  const refreshState = useCallback(async () => {
    try {
      const data = await api.state();
      if (mountedRef.current) setTick(data);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    let timer = null;

    const connect = async () => {
      if (!mountedRef.current) return;
      setReconnecting(true);
      await refreshState();

      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!mountedRef.current) return;
        setConnected(true);
        setReconnecting(false);
        backoffRef.current = 1000;
      };

      ws.onmessage = (ev) => {
        try {
          setTick(JSON.parse(ev.data));
        } catch {
          /* ignore */
        }
      };

      ws.onclose = () => {
        if (!mountedRef.current) return;
        setConnected(false);
        setReconnecting(true);
        const delay = backoffRef.current;
        backoffRef.current = Math.min(30000, delay * 2);
        timer = setTimeout(connect, delay);
      };

      ws.onerror = () => ws.close();
    };

    connect();

    return () => {
      mountedRef.current = false;
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [refreshState]);

  return { tick, connected, reconnecting, refreshState };
}
