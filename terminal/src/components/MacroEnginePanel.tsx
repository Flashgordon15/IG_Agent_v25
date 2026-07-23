"use client";

import { memo } from "react";
import { CorrelationMatrix } from "@/components/CorrelationMatrix";
import { OrderTicket } from "@/components/OrderTicket";
import { SysStreamPending } from "@/components/SysStreamPending";
import type { EngineRuntime } from "@/components/DeskStatusBar";
import type { WsState } from "@/hooks/useAgentTelemetry";
import { buildCorrelationMatrix } from "@/lib/fulfillment-parse";
import type { FulfillmentPayload } from "@/lib/agent-client";
import { agentHttpBase } from "@/lib/agent-client";

type Props = {
  fulfillment: FulfillmentPayload | null;
  focusEpic: string;
  fault: boolean;
  wsState: WsState;
  tradingHealthy: boolean | null;
  engineRuntime: EngineRuntime;
  positionsVerdict: string;
};

function MacroEnginePanelImpl({
  fulfillment,
  focusEpic,
  fault,
  wsState,
  tradingHealthy,
  engineRuntime,
  positionsVerdict,
}: Props) {
  const { labels, matrix } = buildCorrelationMatrix(fulfillment);
  const feed =
    wsState === "live" ? "LIVE" : wsState === "stale" ? "STALE" : wsState.toUpperCase();
  const verdict = (positionsVerdict || "—").toUpperCase();

  return (
    <div className="relative flex h-full min-h-0 flex-col" style={{ gap: "var(--desk-gap)" }}>
      <SysStreamPending active={fault} />
      <div
        className="cq-panel flex shrink-0 items-center justify-between"
        style={{ padding: "0.65rem 0.9rem" }}
      >
        <div>
          <div className="cq-label">Macro Engine</div>
          <div className="mt-0.5 text-[length:var(--desk-type-sm)] font-semibold">
            Execution stack
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="cq-chip">
            <span
              className={
                engineRuntime === "ACTIVE"
                  ? "cq-status-dot cq-status-dot-live"
                  : engineRuntime === "ERROR"
                    ? "cq-status-dot cq-status-dot-danger"
                    : "cq-status-dot cq-status-dot-warn"
              }
            />
            ENGINE {engineRuntime}
          </span>
          <span className="cq-chip">
            <span
              className={
                wsState === "live"
                  ? "cq-status-dot cq-status-dot-live"
                  : "cq-status-dot cq-status-dot-warn"
              }
            />
            FEED {feed}
          </span>
          <span
            className={`cq-chip ${
              tradingHealthy && verdict === "HEALTHY"
                ? "text-[var(--color-desk-good)]"
                : verdict === "CRITICAL"
                  ? "text-[var(--color-desk-bad)]"
                  : "text-[var(--color-desk-warn)]"
            }`}
          >
            {tradingHealthy ? "OK" : "DEG"} · {verdict}
          </span>
        </div>
      </div>
      <div className="min-h-0 shrink-0" style={{ height: "38%" }}>
        <CorrelationMatrix labels={labels} matrix={matrix} fault={false} />
      </div>
      <div className="min-h-0 flex-1">
        <OrderTicket defaultEpic={focusEpic} />
      </div>
      <div
        className="cq-mono shrink-0 rounded-[var(--desk-radius)] border border-[var(--color-desk-line)] bg-[var(--color-desk-panel)] px-3 py-1.5 text-[length:var(--desk-type-xs)] text-[var(--color-desk-mute)]"
      >
        {agentHttpBase()} · {fulfillment?.updated_at ?? "—"}
      </div>
    </div>
  );
}

export const MacroEnginePanel = memo(MacroEnginePanelImpl);
