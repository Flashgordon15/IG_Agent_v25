"use client";

import { CorrelationMatrix } from "@/components/CorrelationMatrix";
import { OrderTicket } from "@/components/OrderTicket";
import { SysStreamPending } from "@/components/SysStreamPending";
import { buildCorrelationMatrix } from "@/lib/fulfillment-parse";
import type { FulfillmentPayload } from "@/lib/agent-client";
import { agentHttpBase } from "@/lib/agent-client";

type Props = {
  fulfillment: FulfillmentPayload | null;
  focusEpic: string;
  fault: boolean;
  wsState: string;
  tradingHealthy: boolean | null;
};

export function MacroEnginePanel({
  fulfillment,
  focusEpic,
  fault,
  wsState,
  tradingHealthy,
}: Props) {
  const { labels, matrix } = buildCorrelationMatrix(fulfillment);

  return (
    <div className="relative col-span-3 flex h-full min-h-0 flex-col gap-1">
      <SysStreamPending active={fault} />
      <div className="cq-panel flex shrink-0 items-center justify-between px-2 py-1">
        <span className="cq-label">Macro Engine</span>
        <div className="flex items-center gap-2">
          <span
            className={`cq-mono text-[9px] ${
              wsState === "live" ? "text-[#00f5d4]" : "text-[#ff9f1c]"
            }`}
          >
            WS:{wsState.toUpperCase()}
          </span>
          <span
            className={`cq-mono text-[9px] ${
              tradingHealthy ? "text-[#00f5d4]" : "text-[#ff0055]"
            }`}
          >
            {tradingHealthy ? "EXEC:OK" : "EXEC:DEG"}
          </span>
        </div>
      </div>
      <div className="min-h-0 shrink-0" style={{ height: "42%" }}>
        <CorrelationMatrix labels={labels} matrix={matrix} fault={false} />
      </div>
      <div className="min-h-0 flex-1">
        <OrderTicket defaultEpic={focusEpic} />
      </div>
      <div className="cq-mono shrink-0 border border-[#1f1f24] bg-[#0f0f12] px-2 py-0.5 text-[9px] text-[#71717a]">
        {agentHttpBase()} · {fulfillment?.updated_at ?? "—"}
      </div>
    </div>
  );
}
