"use client";

import { CandlestickFrame } from "@/components/CandlestickFrame";
import { OrderBookLadder } from "@/components/OrderBookLadder";
import { TransactionLedger } from "@/components/TransactionLedger";
import {
  buildLedgerRows,
  buildOrderBookDepth,
} from "@/lib/fulfillment-parse";
import type { FulfillmentPayload } from "@/lib/agent-client";
import type { LedgerRow } from "@/lib/fulfillment-parse";

type Props = {
  fulfillment: FulfillmentPayload | null;
  focusEpic: string;
  focusHistory: number[];
  triageRows: Array<Record<string, unknown>>;
  tradeRows: Array<Record<string, unknown>>;
  ledgerFault: boolean;
};

export function DeepCoreCanvas({
  fulfillment,
  focusEpic,
  focusHistory,
  triageRows,
  tradeRows,
  ledgerFault,
}: Props) {
  const depth = buildOrderBookDepth(fulfillment, focusEpic);
  const ledger: LedgerRow[] = buildLedgerRows(
    fulfillment,
    triageRows,
    tradeRows,
  );

  return (
    <div
      className="col-span-7 grid h-full min-h-0 grid-rows-[68fr_32fr]"
      style={{ gap: "var(--desk-gap)" }}
    >
      <div className="flex min-h-0" style={{ gap: "var(--desk-gap)" }}>
        <CandlestickFrame epic={focusEpic} history={focusHistory} />
        <OrderBookLadder
          epic={focusEpic}
          asks={depth.asks}
          bids={depth.bids}
        />
      </div>
      <div className="min-h-0">
        <TransactionLedger rows={ledger} fault={ledgerFault} />
      </div>
    </div>
  );
}
