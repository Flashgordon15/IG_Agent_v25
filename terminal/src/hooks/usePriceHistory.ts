"use client";

import { useEffect, useState } from "react";
import {
  buildWatchlistRows,
  liveQuoteForEpic,
} from "@/lib/fulfillment-parse";
import type { FulfillmentPayload } from "@/lib/agent-client";

const MAX_HISTORY = 48;

function midFromPayload(payload: FulfillmentPayload, epic: string): number {
  const q = liveQuoteForEpic(payload, epic);
  if (!q) return 0;
  if (q.mid > 0) return q.mid;
  if (q.last_price > 0) return q.last_price;
  if (q.bid > 0 && q.offer > 0) return (q.bid + q.offer) / 2;
  return q.bid > 0 ? q.bid : q.offer;
}

export function usePriceHistory(fulfillment: FulfillmentPayload | null) {
  const [history, setHistory] = useState<Record<string, number[]>>({});

  useEffect(() => {
    if (!fulfillment) return;
    const epics = [
      ...Object.keys(fulfillment.market_quotes ?? {}),
      ...Object.keys(fulfillment.gate_diagnostics?.by_epic ?? {}),
    ];
    if (!epics.length) return;

    setHistory((prev) => {
      const next = { ...prev };
      for (const epic of epics) {
        const price = midFromPayload(fulfillment, epic);
        if (price <= 0) continue;
        const arr = [...(next[epic] ?? [])];
        const last = arr[arr.length - 1];
        if (last !== price) {
          arr.push(price);
          if (arr.length > MAX_HISTORY) arr.splice(0, arr.length - MAX_HISTORY);
          next[epic] = arr;
        }
      }
      return next;
    });
  }, [fulfillment]);

  const watchlist = buildWatchlistRows(fulfillment, history);
  const focusEpic = watchlist[0]?.epic ?? "CS.D.CFPGOLD.CFP.IP";
  const focusHistory = history[focusEpic] ?? [];

  return { watchlist, focusEpic, focusHistory, history };
}
