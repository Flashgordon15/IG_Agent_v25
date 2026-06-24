"use client";

import { useEffect, useState } from "react";
import {
  fetchAgentJson,
  type TradesPayload,
  type TriageLedgerPayload,
} from "@/lib/agent-client";

const POLL_MS = 1500;

export function useTradeLedger() {
  const [triageRows, setTriageRows] = useState<Array<Record<string, unknown>>>(
    [],
  );
  const [tradeRows, setTradeRows] = useState<Array<Record<string, unknown>>>(
    [],
  );
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(true);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const [triage, trades] = await Promise.all([
          fetchAgentJson<TriageLedgerPayload>(
            "/api/trades/triage-ledger?limit=64",
          ),
          fetchAgentJson<TradesPayload>("/api/trades?limit=32"),
        ]);
        if (!cancelled) {
          setTriageRows(triage.rows ?? []);
          const closed = trades.closed ?? trades.trades ?? [];
          setTradeRows(closed);
          setError(null);
          setPending(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "ledger fetch failed");
          setPending(false);
        }
      }
    };

    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return { triageRows, tradeRows, error, pending };
}
