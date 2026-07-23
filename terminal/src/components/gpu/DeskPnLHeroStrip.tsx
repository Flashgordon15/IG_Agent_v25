"use client";

/**
 * Sticky hero strip — Today's + Weekly realized P&L impossible to miss at desk top.
 */

import { useEffect, useState } from "react";
import { cfdHttpBase, fetchDeskJson, sbHttpBase } from "@/lib/desk-api-bases";
import {
  mergeSimplifiedAccounting,
  type SimplifiedAccountingPayload,
} from "@/lib/desk-accounting-merge";
import {
  mergeWeeklyMetrics,
  type WeeklyMetricsPayload,
} from "@/lib/desk-weekly-metrics-merge";

function fmtGbp(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
}

export function DeskPnLHeroStrip({ openCount = 0 }: { openCount?: number }) {
  const [todayNet, setTodayNet] = useState<number | null>(null);
  const [weekNet, setWeekNet] = useState<number | null>(null);
  const [emptyDay, setEmptyDay] = useState(false);

  useEffect(() => {
    let alive = true;
    const cfdBase = cfdHttpBase();
    const sbBase = sbHttpBase();

    const pull = async () => {
      try {
        const [cfdAcct, sbAcct, cfdWeek, sbWeek] = await Promise.all([
          fetchDeskJson<SimplifiedAccountingPayload>(
            cfdBase,
            "/api/desk/simplified_accounting",
            undefined,
            3500,
          )
            .then((p) => ({ ok: true as const, payload: p }))
            .catch(() => ({ ok: false as const, payload: null })),
          fetchDeskJson<SimplifiedAccountingPayload>(
            sbBase,
            "/api/desk/simplified_accounting",
            undefined,
            3500,
          )
            .then((p) => ({ ok: true as const, payload: p }))
            .catch(() => ({ ok: false as const, payload: null })),
          fetchDeskJson<WeeklyMetricsPayload>(
            cfdBase,
            "/api/desk/weekly_metrics",
            undefined,
            3500,
          )
            .then((p) => ({ ok: true as const, payload: p }))
            .catch(() => ({ ok: false as const, payload: null })),
          fetchDeskJson<WeeklyMetricsPayload>(
            sbBase,
            "/api/desk/weekly_metrics",
            undefined,
            3500,
          )
            .then((p) => ({ ok: true as const, payload: p }))
            .catch(() => ({ ok: false as const, payload: null })),
        ]);
        if (!alive) return;
        const merged = mergeSimplifiedAccounting(
          cfdAcct.ok ? cfdAcct.payload : null,
          sbAcct.ok ? sbAcct.payload : null,
        );
        if (merged) {
          setTodayNet(merged.today_net_realized_pnl_gbp ?? 0);
          setEmptyDay(Boolean(merged.empty_day && Math.abs(merged.today_net_realized_pnl_gbp ?? 0) < 1e-9));
        }
        const weekly = mergeWeeklyMetrics(
          cfdWeek.ok ? cfdWeek.payload : null,
          sbWeek.ok ? sbWeek.payload : null,
        );
        setWeekNet(weekly?.merged?.net_pnl_gbp ?? null);
      } catch {
        /* keep last values */
      }
    };

    void pull();
    const id = window.setInterval(pull, 8000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const todayTone =
    todayNet == null ? "mute" : todayNet >= 0 ? "profit" : "loss";
  const weekTone =
    weekNet == null ? "mute" : weekNet >= 0 ? "profit" : "loss";

  return (
    <section className="desk-pnl-hero" aria-label="Today's and weekly P and L">
      <div className="desk-pnl-hero-inner">
        <div className="desk-pnl-hero-block">
          <span className="desk-pnl-hero-label">Today&apos;s P&amp;L</span>
          <strong
            className={`desk-pnl-hero-value gpu-ledger-mono desk-pnl-hero-value--${todayTone}`}
          >
            {fmtGbp(todayNet)}
          </strong>
          <span className="desk-pnl-hero-hint">Settled cash · realized</span>
        </div>
        <div className="desk-pnl-hero-divider" aria-hidden="true" />
        <div className="desk-pnl-hero-block">
          <span className="desk-pnl-hero-label">Weekly P&amp;L</span>
          <strong
            className={`desk-pnl-hero-value gpu-ledger-mono desk-pnl-hero-value--${weekTone}`}
          >
            {fmtGbp(weekNet)}
          </strong>
          <span className="desk-pnl-hero-hint">Mon–Sun · dual-engine merge</span>
        </div>
        <div className="desk-pnl-hero-divider desk-pnl-hero-divider--hide-sm" aria-hidden="true" />
        <div className="desk-pnl-hero-block desk-pnl-hero-block--compact">
          <span className="desk-pnl-hero-label">Open book</span>
          <strong className="desk-pnl-hero-value desk-pnl-hero-value--open gpu-ledger-mono">
            {openCount}
          </strong>
          <span className="desk-pnl-hero-hint">
            {openCount === 0
              ? "Flat · capital reconciled"
              : "Live broker positions"}
          </span>
        </div>
      </div>
      {emptyDay ? (
        <p className="desk-pnl-hero-flat-note">
          No settled cash today — closed trades and weekly ledger below
        </p>
      ) : null}
    </section>
  );
}
