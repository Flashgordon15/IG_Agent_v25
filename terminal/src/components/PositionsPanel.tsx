"use client";

import { useCallback, useEffect, useState } from "react";
import { SysStreamPending } from "@/components/SysStreamPending";
import {
  closePosition,
  flattenAllPositions,
  useOpenPositions,
} from "@/hooks/useOpenPositions";
import { fetchAgentJson } from "@/lib/agent-client";
import type { LivePositionRow } from "@/lib/agent-client";

type IntradaySlotsSnap = {
  current_slot_id?: string | null;
  slots?: Record<string, { label?: string; is_current?: boolean }>;
};

function fmtGbp(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}£${v.toFixed(2)}`;
}

function epicShort(epic: string): string {
  const parts = epic.split(".");
  if (parts.length >= 3) return parts[2].slice(0, 8);
  return epic.slice(0, 10);
}

function protectionLayerBadges(pos: LivePositionRow) {
  const ps = pos.protection_summary;
  const g = ps?.gbp_armed ?? pos.gbp_armed;
  const v = ps?.virtual_armed ?? pos.virtual_armed;
  const d = ps?.dynamic_armed ?? pos.dynamic_armed;
  const badge = (on: boolean, label: string) => (
    <span
      key={label}
      className={`cq-mono inline-block rounded px-1 text-[8px] ${
        on
          ? "bg-[#00f5d4]/15 text-[#00f5d4]"
          : "bg-[#ff0055]/15 text-[#ff0055]"
      }`}
      title={on ? `${label} armed` : `${label} missing`}
    >
      {label}
    </span>
  );
  return (
    <span className="inline-flex gap-0.5">
      {badge(Boolean(g), "G")}
      {badge(Boolean(v), "V")}
      {badge(Boolean(d), "D")}
    </span>
  );
}

function formatProtectionRisk(pos: LivePositionRow): string {
  const ps = pos.protection_summary ?? pos;
  const cap = ps.loss_cap_gbp;
  const soft = ps.soft_loss_gbp;
  const floor = ps.trail_floor_gbp;
  const peak = ps.peak_profit_gbp;
  const target = ps.target_gbp;
  if (cap == null && soft == null && floor == null && target == null) return "—";
  const parts: string[] = [];
  if (soft != null) parts.push(`soft ${fmtGbp(-Math.abs(soft))}`);
  if (cap != null) parts.push(`cap ${fmtGbp(-Math.abs(cap))}`);
  if (floor != null && Number(floor) > 0) parts.push(`floor ${fmtGbp(floor)}`);
  else if (peak != null && Number(peak) > 0) parts.push(`peak ${fmtGbp(peak)}`);
  if (target != null) parts.push(`tgt ${fmtGbp(target)}`);
  return parts.join(" · ") || "—";
}

type PositionsPanelProps = {
  /** Lifted from TerminalShell so status bar + table share one poll. */
  positions: ReturnType<typeof useOpenPositions>;
};

export function PositionsPanel({ positions }: PositionsPanelProps) {
  const { data, liveness, error, pending, refresh } = positions;
  const [closingId, setClosingId] = useState<string | null>(null);
  const [flattening, setFlattening] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [slotLabel, setSlotLabel] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const snap = await fetchAgentJson<IntradaySlotsSnap>(
          "/api/strategy/intraday_slots",
          undefined,
          3000,
        );
        if (cancelled) return;
        const sid = snap.current_slot_id;
        const slot =
          (sid && snap.slots?.[sid]) ||
          Object.values(snap.slots ?? {}).find((s) => s.is_current);
        setSlotLabel(slot?.label ?? sid ?? null);
      } catch {
        if (!cancelled) setSlotLabel(null);
      }
    };
    void poll();
    const id = window.setInterval(poll, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const rows = data?.positions ?? [];
  const count = data?.count ?? 0;
  const totalPnl = data?.total_pnl_gbp ?? 0;
  const unmonitored = data?.unmonitored ?? 0;
  const verdict = data?.verdict ?? "—";
  const critical = Boolean(data?.critical) || verdict === "CRITICAL";
  const flattenErr = data?.trade_support?.last_flatten_error;
  const deskOk = liveness?.ok !== false && !critical;
  const fault = Boolean(error) && !pending;

  const onClose = useCallback(
    async (dealId: string) => {
      setActionError(null);
      setClosingId(dealId);
      try {
        await closePosition(dealId);
        await refresh();
      } catch (e) {
        setActionError(e instanceof Error ? e.message : "close failed");
      } finally {
        setClosingId(null);
      }
    },
    [refresh],
  );

  const onFlattenAll = useCallback(async () => {
    if (!window.confirm(`Flatten all ${count} open positions?`)) return;
    setActionError(null);
    setFlattening(true);
    try {
      await flattenAllPositions();
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "flatten failed");
    } finally {
      setFlattening(false);
    }
  }, [count, refresh]);

  return (
    <div className="relative cq-panel flex h-full min-h-0 flex-col overflow-hidden">
      <SysStreamPending active={pending && !data} />
      <div className="flex shrink-0 items-center justify-between border-b border-[var(--color-desk-line)] px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <div>
            <div className="cq-label">Positions</div>
            <div className="text-[length:var(--desk-type-sm)] font-semibold">
              Open book <span className="cq-mono text-[var(--color-desk-mute)]">({count})</span>
            </div>
          </div>
          {critical && (
            <span className="cq-mono text-[9px] text-[#f43f5e]">CRITICAL</span>
          )}
          {unmonitored > 0 && (
            <span className="cq-mono text-[9px] text-[#f43f5e]">
              {unmonitored} UNARMED
            </span>
          )}
          {!deskOk && !critical && (
            <span className="cq-mono text-[9px] text-[#fbbf24]">
              RECOVERING
            </span>
          )}
          {slotLabel && (
            <span
              className="cq-mono text-[9px] text-[#5a6478]"
              title="BST intraday session slot"
            >
              Slot: {slotLabel}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`cq-mono text-[10px] ${
              totalPnl >= 0 ? "text-[#34d399]" : "text-[#f43f5e]"
            }`}
          >
            {fmtGbp(totalPnl)}
          </span>
          <span
            className={`cq-mono text-[9px] ${
              verdict === "HEALTHY"
                ? "text-[#34d399]"
                : verdict === "FLAT"
                  ? "text-[#5a6478]"
                  : verdict === "CRITICAL"
                    ? "text-[#f43f5e]"
                    : "text-[#fbbf24]"
            }`}
          >
            {verdict}
          </span>
          <button
            type="button"
            disabled={count === 0 || flattening}
            onClick={onFlattenAll}
            className="cq-mono rounded-sm border border-[#f43f5e]/40 px-2 py-1 text-[9px] text-[#f43f5e] hover:bg-[#f43f5e]/10 disabled:opacity-30"
          >
            {flattening ? "FLATTEN…" : "FLATTEN ALL"}
          </button>
        </div>
      </div>

      {(critical || flattenErr || data?.protection_note) && (
        <div className="cq-mono shrink-0 bg-[#ff0055]/10 px-2 py-0.5 text-[9px] text-[#ff0055]">
          {flattenErr
            ? `Flatten failed: ${flattenErr}`
            : (data?.protection_note ?? "Critical open-risk alarm")}
        </div>
      )}

      {(error || actionError || fault) && !critical && (
        <div className="cq-mono shrink-0 bg-[#ff0055]/10 px-2 py-0.5 text-[9px] text-[#ff0055]">
          {actionError ?? error}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {count === 0 && !pending ? (
          <div className="cq-mono flex h-full items-center justify-center text-[10px] text-[#52525b]">
            No open positions
          </div>
        ) : (
          <table className="w-full border-collapse text-left">
            <thead className="sticky top-0 bg-[#0a0a0c]">
              <tr className="cq-mono text-[8px] uppercase tracking-wide text-[#52525b]">
                <th className="px-2 py-1 font-normal">Epic</th>
                <th className="px-1 py-1 font-normal">Dir</th>
                <th className="px-1 py-1 font-normal">Sz</th>
                <th className="px-1 py-1 font-normal">P&amp;L</th>
                <th className="px-1 py-1 font-normal">G/V/D</th>
                <th className="px-1 py-1 font-normal">Risk £</th>
                <th className="px-2 py-1 font-normal text-right">Act</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const pnl = p.pnl_gbp;
                const pnlClass =
                  pnl == null
                    ? "text-[#71717a]"
                    : pnl >= 0
                      ? "text-[#00f5d4]"
                      : "text-[#ff0055]";
                const busy = closingId === p.deal_id;
                return (
                  <tr
                    key={p.deal_id}
                    className="border-t border-[#1f1f24]/60 hover:bg-[#141418]"
                  >
                    <td className="cq-mono px-2 py-1 text-[10px] text-[#e4e4e7]">
                      {epicShort(p.epic)}
                      {p.flatten_failed && (
                        <div className="text-[8px] text-[#ff0055]">FLAT FAIL</div>
                      )}
                    </td>
                    <td
                      className={`cq-mono px-1 py-1 text-[10px] ${
                        p.direction === "BUY"
                          ? "text-[#00f5d4]"
                          : "text-[#ff9f1c]"
                      }`}
                    >
                      {p.direction === "BUY" ? "L" : "S"}
                    </td>
                    <td className="cq-mono px-1 py-1 text-[10px] text-[#a1a1aa]">
                      {p.size}
                    </td>
                    <td className={`cq-mono px-1 py-1 text-[10px] ${pnlClass}`}>
                      {fmtGbp(pnl)}
                    </td>
                    <td className="px-1 py-1">{protectionLayerBadges(p)}</td>
                    <td
                      className="cq-mono px-1 py-1 text-[8px] text-[#71717a]"
                      title={p.protection_summary?.operator_note ?? undefined}
                    >
                      {formatProtectionRisk(p)}
                    </td>
                    <td className="px-2 py-1 text-right">
                      <button
                        type="button"
                        disabled={busy || flattening}
                        onClick={() => onClose(p.deal_id)}
                        className="cq-mono rounded border border-[#52525b] px-1.5 py-0.5 text-[9px] text-[#e4e4e7] hover:border-[#ff0055] hover:text-[#ff0055] disabled:opacity-40"
                      >
                        {busy ? "…" : "CLOSE"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
