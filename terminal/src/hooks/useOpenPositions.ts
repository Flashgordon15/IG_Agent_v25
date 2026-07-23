"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  agentHttpBase,
  fetchAgentJson,
  type LivePositionsPayload,
  type TradingDeskLiveness,
} from "@/lib/agent-client";

const POLL_MS = 5000;
const LIVENESS_POLL_MS = 10000;
const RECOVER_COOLDOWN_MS = 30000;

export function useOpenPositions() {
  const [data, setData] = useState<LivePositionsPayload | null>(null);
  const [liveness, setLiveness] = useState<TradingDeskLiveness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(true);
  const inflight = useRef(false);
  const lastRecover = useRef(0);

  const maybeRecover = useCallback(async (liv: TradingDeskLiveness | null) => {
    if (!liv || liv.ok) return;
    const now = Date.now();
    if (now - lastRecover.current < RECOVER_COOLDOWN_MS) return;
    if (!liv.has_open_risk && (liv.unmonitored ?? 0) === 0) return;
    lastRecover.current = now;
    try {
      await fetch(`${agentHttpBase()}/api/trading_desk/recover`, {
        method: "POST",
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(8000),
      });
    } catch {
      /* recovery is best-effort */
    }
  }, []);

  const refreshLiveness = useCallback(async () => {
    try {
      const liv = await fetchAgentJson<TradingDeskLiveness>(
        "/api/trading_desk/liveness",
        undefined,
        3000,
      );
      setLiveness(liv);
      await maybeRecover(liv);
    } catch {
      setLiveness({ ok: false, issues: ["liveness_unreachable"] });
    }
  }, [maybeRecover]);

  const refresh = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const payload = await fetchAgentJson<LivePositionsPayload>(
        "/api/positions/live",
        undefined,
        3500,
      );
      setData(payload);
      const degraded =
        !payload.ok ||
        payload.verdict === "DEGRADED" ||
        payload.verdict === "CRITICAL" ||
        Boolean(payload.critical) ||
        Boolean(payload.stale) ||
        (payload.unmonitored ?? 0) > 0;
      const alarm =
        payload.trade_support?.last_flatten_error ||
        payload.critical_alarms?.[0] ||
        payload.error ||
        payload.verdict ||
        "positions degraded";
      setError(payload.ok && !degraded ? null : String(alarm));
      setPending(false);
      if (degraded) {
        void refreshLiveness();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "positions fetch failed");
      setPending(false);
      void refreshLiveness();
    } finally {
      inflight.current = false;
    }
  }, [refreshLiveness]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    const lid = setInterval(refreshLiveness, LIVENESS_POLL_MS);
    return () => {
      clearInterval(id);
      clearInterval(lid);
    };
  }, [refresh, refreshLiveness]);

  return { data, liveness, error, pending, refresh };
}

export async function closePosition(dealId: string): Promise<void> {
  const res = await fetch(`${agentHttpBase()}/api/close/${encodeURIComponent(dealId)}`, {
    method: "POST",
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(15000),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `close HTTP ${res.status}`);
  }
}

export async function flattenAllPositions(): Promise<{ count: number }> {
  const res = await fetch(`${agentHttpBase()}/api/flatten/all`, {
    method: "POST",
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) {
    throw new Error(`flatten HTTP ${res.status}`);
  }
  const body = (await res.json()) as { count?: number };
  return { count: body.count ?? 0 };
}
