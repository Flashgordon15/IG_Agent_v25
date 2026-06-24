"use client";

import { useEffect, useState } from "react";
import { AdaptiveLogisticsPanel } from "@/components/AdaptiveLogisticsPanel";
import { DeepCoreCanvas } from "@/components/DeepCoreCanvas";
import { MacroEnginePanel } from "@/components/MacroEnginePanel";
import { WatchlistMatrix } from "@/components/WatchlistMatrix";
import { useAgentTelemetry } from "@/hooks/useAgentTelemetry";
import { useFulfillment } from "@/hooks/useFulfillment";
import { usePriceHistory } from "@/hooks/usePriceHistory";
import { useTradeLedger } from "@/hooks/useTradeLedger";
import { fetchAgentJson, type AgentHealth } from "@/lib/agent-client";

export function TerminalShell() {
  const { wsState } = useAgentTelemetry();
  const fulfillmentState = useFulfillment();
  const { triageRows, tradeRows, error: ledgerError, pending: ledgerPending } =
    useTradeLedger();
  const { watchlist, focusEpic: defaultEpic, focusHistory, history } =
    usePriceHistory(fulfillmentState.data);
  const [selectedEpic, setSelectedEpic] = useState(defaultEpic);
  const [tradingHealthy, setTradingHealthy] = useState<boolean | null>(null);

  useEffect(() => {
    setSelectedEpic((prev) =>
      watchlist.some((w) => w.epic === prev) ? prev : defaultEpic,
    );
  }, [defaultEpic, watchlist]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const h = await fetchAgentJson<AgentHealth>("/api/health");
        if (!cancelled) setTradingHealthy(Boolean(h.trading_healthy));
      } catch {
        if (!cancelled) setTradingHealthy(null);
      }
    };
    poll();
    const id = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const fulfillmentFault =
    fulfillmentState.pending ||
    Boolean(fulfillmentState.error) ||
    fulfillmentState.stale ||
    wsState === "offline" ||
    wsState === "stale";

  const ledgerFault =
    ledgerPending || Boolean(ledgerError) || fulfillmentFault;

  const activeHistory = history[selectedEpic] ?? focusHistory;

  return (
    <div className="grid h-screen grid-rows-[auto_1fr] gap-1 overflow-hidden bg-[#050505] p-2">
      <AdaptiveLogisticsPanel focusEpic={selectedEpic} fault={fulfillmentFault} />

      <div className="grid min-h-0 grid-cols-12 gap-1">
      <div className="col-span-2 min-h-0">
        <WatchlistMatrix
          rows={watchlist}
          selectedEpic={selectedEpic}
          onSelect={setSelectedEpic}
          fault={fulfillmentFault}
        />
      </div>

      <DeepCoreCanvas
        fulfillment={fulfillmentState.data}
        focusEpic={selectedEpic}
        focusHistory={activeHistory}
        triageRows={triageRows}
        tradeRows={tradeRows}
        ledgerFault={ledgerFault}
      />

      <MacroEnginePanel
        fulfillment={fulfillmentState.data}
        focusEpic={selectedEpic}
        fault={fulfillmentFault}
        wsState={wsState}
        tradingHealthy={tradingHealthy}
      />
      </div>
    </div>
  );
}
