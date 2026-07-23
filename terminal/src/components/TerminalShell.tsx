"use client";

import { useEffect, useMemo, useState } from "react";
import { AdaptiveLogisticsPanel } from "@/components/AdaptiveLogisticsPanel";
import { DeepCoreCanvas } from "@/components/DeepCoreCanvas";
import { DeskStatusBar, type EngineRuntime } from "@/components/DeskStatusBar";
import { MacroEnginePanel } from "@/components/MacroEnginePanel";
import { PositionsPanel } from "@/components/PositionsPanel";
import { WatchlistMatrix } from "@/components/WatchlistMatrix";
import { useAgentTelemetry } from "@/hooks/useAgentTelemetry";
import { useFulfillment } from "@/hooks/useFulfillment";
import { useOpenPositions } from "@/hooks/useOpenPositions";
import { usePriceHistory } from "@/hooks/usePriceHistory";
import { useTradeLedger } from "@/hooks/useTradeLedger";
import { fetchAgentJson, type AgentHealth } from "@/lib/agent-client";

export function TerminalShell() {
  const { wsState } = useAgentTelemetry();
  const fulfillmentState = useFulfillment();
  const positionsShared = useOpenPositions();
  const positionsData = positionsShared.data;
  const { triageRows, tradeRows, error: ledgerError, pending: ledgerPending } =
    useTradeLedger();
  const { watchlist, focusEpic: defaultEpic, focusHistory, history } =
    usePriceHistory(fulfillmentState.data);
  const [selectedEpic, setSelectedEpic] = useState(defaultEpic);
  const [tradingHealthy, setTradingHealthy] = useState<boolean | null>(null);
  const [loopsRunning, setLoopsRunning] = useState<boolean | null>(null);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    setSelectedEpic((prev) =>
      watchlist.some((w) => w.epic === prev) ? prev : defaultEpic,
    );
  }, [defaultEpic, watchlist]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const h = await fetchAgentJson<
          AgentHealth & {
            trading_loops_running?: boolean;
            paused?: boolean;
            trading_paused?: boolean;
          }
        >("/api/health");
        if (cancelled) return;
        setTradingHealthy(Boolean(h.trading_healthy));
        if (typeof h.trading_loops_running === "boolean") {
          setLoopsRunning(h.trading_loops_running);
        }
        const pausedFlag =
          typeof h.trading_paused === "boolean"
            ? Boolean(h.trading_paused)
            : typeof h.paused === "boolean"
              ? h.paused
              : false;
        setPaused(pausedFlag);
      } catch {
        if (!cancelled) {
          setTradingHealthy(null);
          setLoopsRunning(null);
        }
      }
    };
    void poll();
    const id = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const feedStale =
    fulfillmentState.stale || wsState === "stale" || wsState === "offline";

  const fulfillmentFault =
    fulfillmentState.pending ||
    Boolean(fulfillmentState.error) ||
    fulfillmentState.stale ||
    wsState === "offline" ||
    wsState === "stale";

  const ledgerFault =
    ledgerPending || Boolean(ledgerError) || fulfillmentFault;

  const activeHistory = history[selectedEpic] ?? focusHistory;

  const engineRuntime: EngineRuntime = useMemo(() => {
    if (tradingHealthy === null && loopsRunning === null) return "ERROR";
    if (paused) return "PAUSED";
    if (tradingHealthy === false) return "ERROR";
    if (loopsRunning === false) return "IDLE";
    if (tradingHealthy || loopsRunning) return "ACTIVE";
    return "IDLE";
  }, [tradingHealthy, loopsRunning, paused]);

  const positionsVerdict = positionsData?.verdict ?? "—";

  return (
    <div
      className="cq-shell cq-fade-in grid h-screen overflow-hidden"
      style={{
        gridTemplateRows: "auto auto minmax(0, 1fr)",
        gap: "var(--desk-gap)",
        padding: "var(--desk-pad)",
      }}
    >
      <AdaptiveLogisticsPanel
        focusEpic={selectedEpic}
        fault={fulfillmentFault}
      />

      <DeskStatusBar
        engineRuntime={engineRuntime}
        tradingHealthy={tradingHealthy}
        positionsVerdict={positionsVerdict}
        wsState={wsState}
        feedStale={feedStale}
      />

      <div
        className="grid min-h-0 grid-cols-12"
        style={{ gap: "var(--desk-gap)" }}
      >
        <div className="col-span-2 min-h-0 2xl:col-span-2">
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

        <div
          className="col-span-3 flex h-full min-h-0 flex-col"
          style={{ gap: "var(--desk-gap)" }}
        >
          <div className="min-h-0 flex-[3.2]">
            <PositionsPanel positions={positionsShared} />
          </div>
          <div className="min-h-0 flex-[2]">
            <MacroEnginePanel
              fulfillment={fulfillmentState.data}
              focusEpic={selectedEpic}
              fault={fulfillmentFault}
              wsState={wsState}
              tradingHealthy={tradingHealthy}
              engineRuntime={engineRuntime}
              positionsVerdict={positionsVerdict}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
