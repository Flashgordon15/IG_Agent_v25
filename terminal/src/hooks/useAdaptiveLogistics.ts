"use client";

import { useEffect, useState } from "react";
import {
  buildAdaptiveLogisticsSnapshot,
  type AdaptiveLogisticsSnapshot,
  type StarvationTuneState,
} from "@/lib/adaptive-logistics-parse";
import {
  fetchAgentJson,
  type FulfillmentPayload,
} from "@/lib/agent-client";

const POLL_MS = 1500;

export function useAdaptiveLogistics(focusEpic: string) {
  const [snapshot, setSnapshot] = useState<AdaptiveLogisticsSnapshot | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [pollTick, setPollTick] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const [fulfillment, telemetry, tune] = await Promise.all([
          fetchAgentJson<FulfillmentPayload>("/api/unified/fulfillment"),
          fetch("/api/adaptive-telemetry", { cache: "no-store" }).then((r) =>
            r.json(),
          ) as Promise<{ log_tail?: string }>,
          fetchAgentJson<{ ok?: boolean } & StarvationTuneState>(
            "/api/v31/tune",
          ).catch(() => null),
        ]);
        if (cancelled) return;
        const built = buildAdaptiveLogisticsSnapshot(
          fulfillment,
          String(telemetry.log_tail ?? ""),
          focusEpic,
          tune,
        );
        setSnapshot(built);
        setError(null);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "adaptive logistics poll failed");
        }
      }
    };

    void poll();
    const id = setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [focusEpic, pollTick]);

  return {
    snapshot,
    error,
    refresh: () => setPollTick((t) => t + 1),
  };
}
