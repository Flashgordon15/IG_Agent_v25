"use client";

import { useEffect, useRef, useState } from "react";
import { FULFILLMENT_STALE_MS } from "@/lib/constants";
import {
  fetchAgentJson,
  type FulfillmentPayload,
} from "@/lib/agent-client";

const POLL_MS = 500;
const STRESS_POLL_MS = 20;
const POLL_TIMEOUT_MS = 8_000;
const KINETIC_DELTA_PCT = 0.05;
const KINETIC_WINDOW_MS = 20;

export type FulfillmentState = {
  data: FulfillmentPayload | null;
  error: string | null;
  pending: boolean;
  stale: boolean;
  lastOkAt: number;
};

export function useFulfillment(): FulfillmentState {
  const [data, setData] = useState<FulfillmentPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(true);
  const [lastOkAt, setLastOkAt] = useState(0);
  const [stale, setStale] = useState(false);
  const inflightRef = useRef(false);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    let pollMs = POLL_MS;

    const scheduleNext = (delayMs = pollMs) => {
      if (cancelled || inflightRef.current) return;
      if (pollTimerRef.current !== null) {
        clearTimeout(pollTimerRef.current);
      }
      pollTimerRef.current = setTimeout(() => {
        pollTimerRef.current = null;
        void poll();
      }, delayMs);
    };

    const poll = async () => {
      if (cancelled || inflightRef.current) {
        return;
      }
      inflightRef.current = true;
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
      try {
        const payload = await fetchAgentJson<FulfillmentPayload>(
          "/api/unified/fulfillment",
          { signal: controller.signal },
        );
        if (!cancelled) {
          pollMs = payload.ui_stress_render?.active
            ? Number(payload.ui_stress_render.poll_ms ?? STRESS_POLL_MS)
            : POLL_MS;
          setData(payload);
          setError(null);
          setPending(false);
          setLastOkAt(Date.now());
          setStale(false);
        }
      } catch (e) {
        if (!cancelled) {
          const message =
            e instanceof Error && e.name === "AbortError"
              ? "fulfillment poll timed out"
              : e instanceof Error
                ? e.message
                : "fulfillment poll failed";
          setError(message);
          setPending(false);
        }
      } finally {
        clearTimeout(timeoutId);
        inflightRef.current = false;
        if (!cancelled) {
          scheduleNext(POLL_MS);
        }
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (pollTimerRef.current !== null) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      if (lastOkAt > 0 && Date.now() - lastOkAt > FULFILLMENT_STALE_MS) {
        setStale(true);
      }
    }, 500);
    return () => clearInterval(id);
  }, [lastOkAt]);

  return { data, error, pending, stale, lastOkAt };
}
