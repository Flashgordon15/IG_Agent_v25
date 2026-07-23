"use client";

import { useEffect, useRef } from "react";
import { cfdHttpBase, fetchDeskJson, sbHttpBase } from "@/lib/desk-api-bases";
import {
  mergeSimplifiedAccounting,
  type ClosedTradeRow,
  type DailyHistoryRow,
  type SimplifiedAccountingPayload,
} from "@/lib/desk-accounting-merge";

export type { ClosedTradeRow, DailyHistoryRow };

export type SimplifiedAccounting = SimplifiedAccountingPayload;

type Props = {
  onHealth?: (state: SimplifiedAccounting["system_state"]) => void;
};

const ACCOUNTING_POLL_MS = 15_000;
const MAX_ROWS = 10;

const ENGINE_ACCOUNT: Record<string, { accountId: string; productType: string }> = {
  CFD: { accountId: "Z6BAH4", productType: "CFD" },
  SB: { accountId: "Z6BAH3", productType: "SPREADBET" },
  QUANT_SNIPER: { accountId: "Z6BAH4", productType: "CFD" },
  MACRO_SENTINEL: { accountId: "Z6BAH3", productType: "SPREADBET" },
};

function fmtGbp(v: number): string {
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
}

function resolveAccount(row: ClosedTradeRow): string {
  if (row.account_id) return row.account_id;
  const tag = row.engine_label || row.engine_origin || "";
  return ENGINE_ACCOUNT[tag]?.accountId || "—";
}

function resolveProduct(row: ClosedTradeRow): string {
  if (row.product_type) return row.product_type;
  const tag = row.engine_label || row.engine_origin || "";
  return ENGINE_ACCOUNT[tag]?.productType || "—";
}

type RowRefs = {
  ts: HTMLTableCellElement | null;
  asset: HTMLTableCellElement | null;
  account: HTMLTableCellElement | null;
  product: HTMLTableCellElement | null;
  pnl: HTMLTableCellElement | null;
};

export function SovereignAccountingBoard({ onHealth }: Props) {
  const cashRef = useRef<HTMLSpanElement | null>(null);
  const sourceRef = useRef<HTMLSpanElement | null>(null);
  const rowRefs = useRef<RowRefs[]>(
    Array.from({ length: MAX_ROWS }, () => ({
      ts: null,
      asset: null,
      account: null,
      product: null,
      pnl: null,
    })),
  );
  const emptyRef = useRef<HTMLTableRowElement | null>(null);
  const lastCashRef = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    const cfdBase = cfdHttpBase();
    const sbBase = sbHttpBase();

    const paintRows = (trades: ClosedTradeRow[]) => {
      const sorted = [...trades].sort((a, b) =>
        String(b.timestamp || "").localeCompare(String(a.timestamp || "")),
      );
      if (emptyRef.current) {
        emptyRef.current.hidden = sorted.length > 0;
      }
      for (let i = 0; i < MAX_ROWS; i += 1) {
        const refs = rowRefs.current[i];
        const row = sorted[i];
        const tr = refs.ts?.parentElement as HTMLTableRowElement | null;
        if (tr) tr.hidden = !row;
        if (!row || !refs.ts) continue;
        refs.ts.textContent = row.timestamp || "—";
        if (refs.asset) refs.asset.textContent = row.asset || "—";
        if (refs.account) refs.account.textContent = resolveAccount(row);
        if (refs.product) refs.product.textContent = resolveProduct(row);
        if (refs.pnl) {
          refs.pnl.textContent = fmtGbp(row.net_pnl_gbp);
          refs.pnl.className = `gpu-ledger-mono v36-acct-pnl ${
            row.net_pnl_gbp >= 0 ? "sovereign-pnl--up" : "sovereign-pnl--down"
          }`;
        }
      }
    };

    const pull = async () => {
      const [cfdRes, sbRes] = await Promise.all([
        fetchDeskJson<SimplifiedAccounting>(
          cfdBase,
          "/api/desk/simplified_accounting",
          undefined,
          3500,
        )
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
        fetchDeskJson<SimplifiedAccounting>(
          sbBase,
          "/api/desk/simplified_accounting",
          undefined,
          3500,
        )
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
      ]);
      if (!alive) return;

      const merged = mergeSimplifiedAccounting(
        cfdRes.ok ? cfdRes.payload : null,
        sbRes.ok ? sbRes.payload : null,
      );
      if (!merged) return;

      const today = merged.today_net_realized_pnl_gbp ?? 0;
      const prevCash = lastCashRef.current;
      const nextRows = merged.last_10_closed_trades ?? [];
      const nextHasCash =
        Math.abs(today) > 1e-9 ||
        nextRows.some((r) => Math.abs(r.net_pnl_gbp) > 1e-9);
      const prevHasCash =
        prevCash != null &&
        (Math.abs(prevCash) > 1e-9 ||
          rowRefs.current.some((r) => r.pnl && r.pnl.textContent !== "—"));

      if (!nextHasCash && prevHasCash) return;

      lastCashRef.current = today;
      if (cashRef.current) {
        cashRef.current.textContent = fmtGbp(today);
        cashRef.current.className = `v36-acct-cash gpu-ledger-mono ${
          today >= 0 ? "sovereign-pnl--up" : "sovereign-pnl--down"
        }`;
      }
      if (sourceRef.current) {
        sourceRef.current.textContent = !merged
          ? "WAITING"
          : merged.source === "dual_merged"
            ? `DUAL · ${cfdRes.ok ? "8080✓" : "8080↓"} ${sbRes.ok ? "8081✓" : "8081↓"}`
            : String(merged.source || "JOURNAL").toUpperCase();
      }
      paintRows(nextRows);
      onHealth?.(merged.system_state);
    };

    void pull();
    const id = window.setInterval(pull, ACCOUNTING_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [onHealth]);

  return (
    <section
      className="v36-accounting-bar desk-section-closed"
      aria-label="True settled cash and last closed trades"
    >
      <div className="v36-accounting-top">
        <div className="v36-accounting-cash-block">
          <span className="v36-acct-kicker">True Settled Cash</span>
          <strong ref={cashRef} className="v36-acct-cash gpu-ledger-mono sovereign-pnl--mute">
            —
          </strong>
          <span className="v36-acct-sub">Today · net realized · dual-port merge</span>
        </div>
        <span ref={sourceRef} className="gpu-chip gpu-chip--mono">
          WAITING
        </span>
      </div>

      <div className="v36-accounting-table-wrap">
        <table className="v36-accounting-table sovereign-table">
          <thead>
            <tr>
              <th>TIMESTAMP</th>
              <th>ASSET</th>
              <th>ACCOUNT ID</th>
              <th>PRODUCT TYPE</th>
              <th>NET REALIZED P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            <tr ref={emptyRef} className="v36-acct-empty-row">
              <td colSpan={5} className="sovereign-empty">
                No settled closes yet
              </td>
            </tr>
            {Array.from({ length: MAX_ROWS }, (_, i) => (
              <tr key={i} hidden>
                <td
                  ref={(el) => {
                    rowRefs.current[i]!.ts = el;
                  }}
                  className="gpu-ledger-mono"
                />
                <td
                  ref={(el) => {
                    rowRefs.current[i]!.asset = el;
                  }}
                />
                <td
                  ref={(el) => {
                    rowRefs.current[i]!.account = el;
                  }}
                  className="gpu-ledger-mono"
                />
                <td
                  ref={(el) => {
                    rowRefs.current[i]!.product = el;
                  }}
                  className="gpu-ledger-mono"
                />
                <td
                  ref={(el) => {
                    rowRefs.current[i]!.pnl = el;
                  }}
                  className="gpu-ledger-mono v36-acct-pnl"
                />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
