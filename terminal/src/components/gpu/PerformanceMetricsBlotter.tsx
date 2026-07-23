"use client";

/**
 * Compact intra-day + weekly performance blotter under ExecutionTruthStrip.
 * Ref-driven cell mutations — no per-poll React setState for metric values.
 */

import { useEffect, useRef } from "react";
import { cfdHttpBase, fetchDeskJson, sbHttpBase } from "@/lib/desk-api-bases";
import {
  mergeSimplifiedAccounting,
  type SimplifiedAccountingPayload,
} from "@/lib/desk-accounting-merge";
import {
  formatAssetBreakdown,
  mergeWeeklyMetrics,
  type WeeklyMetricsPayload,
} from "@/lib/desk-weekly-metrics-merge";

type PerfMetrics = NonNullable<SimplifiedAccountingPayload["performance_metrics"]>;

type CellRefs = {
  scope: HTMLSpanElement | null;
  sharpe: HTMLSpanElement | null;
  profitFactor: HTMLSpanElement | null;
  winLoss: HTMLSpanElement | null;
  winRate: HTMLSpanElement | null;
  dayNet: HTMLSpanElement | null;
  weekScope: HTMLSpanElement | null;
  weekSharpe: HTMLSpanElement | null;
  weekPf: HTMLSpanElement | null;
  weekWinRate: HTMLSpanElement | null;
  weekNet: HTMLSpanElement | null;
  weekAssets: HTMLSpanElement | null;
  weekCfd: HTMLSpanElement | null;
  weekSb: HTMLSpanElement | null;
};

function fmt(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

function paintMetrics(cells: CellRefs, perf: PerfMetrics, todayNet: number | null): void {
  const scope = String(perf.sample_scope || "today").toUpperCase();
  const wl =
    perf.true_wins != null && perf.true_losses != null
      ? `${perf.true_wins}W / ${perf.true_losses}L`
      : perf.wins != null && perf.losses != null
        ? `${perf.wins}W / ${perf.losses}L`
        : "—";

  if (cells.scope) {
    cells.scope.textContent = `${scope} · n=${perf.sample_n ?? 0}${
      perf.breakeven_excluded
        ? ` · excl ${perf.breakeven_excluded} BE`
        : ""
    }${
      todayNet != null
        ? ` · DAY ${todayNet >= 0 ? "+" : "−"}£${Math.abs(todayNet).toFixed(2)}`
        : ""
    }`;
  }
  if (cells.sharpe) cells.sharpe.textContent = fmt(perf.intraday_sharpe, 3);
  if (cells.profitFactor) cells.profitFactor.textContent = fmt(perf.profit_factor, 2);
  if (cells.winLoss) {
    cells.winLoss.textContent = fmt(perf.true_win_loss_ratio ?? perf.win_loss_ratio, 2);
    const sub = cells.winLoss.querySelector(".perf-metrics-sub");
    if (sub) sub.textContent = ` · ${wl}`;
  }
  if (cells.winRate) {
    const rate = perf.true_win_rate ?? perf.win_rate;
    cells.winRate.textContent =
      rate != null && Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : "—";
  }
  if (cells.dayNet) {
    const net = perf.net_true_outcome_gbp;
    if (net != null && Number.isFinite(net)) {
      cells.dayNet.textContent = `${net >= 0 ? "+" : "−"}£${Math.abs(net).toFixed(2)}`;
      cells.dayNet.dataset.tone = net >= 0 ? "profit" : net < 0 ? "loss" : "mute";
    } else {
      cells.dayNet.textContent = "—";
      cells.dayNet.dataset.tone = "mute";
    }
  }
}

function paintWeekly(cells: CellRefs, weekly: WeeklyMetricsPayload | null): void {
  const merged = weekly?.merged;
  const weekStart = weekly?.week_start || "—";
  const weekEnd = weekly?.week_end || "—";
  const sampleN = merged?.sample_n ?? 0;
  const tradingDays = merged?.trading_days ?? 0;

  if (cells.weekScope) {
    cells.weekScope.textContent = `${weekStart} → ${weekEnd} · n=${sampleN} · ${tradingDays}d`;
  }
  if (cells.weekSharpe) {
    cells.weekSharpe.textContent = fmt(merged?.weekly_sharpe, 3);
  }
  if (cells.weekPf) {
    cells.weekPf.textContent = fmt(merged?.asymmetric_profit_factor, 2);
  }
  if (cells.weekWinRate) {
    const rate = merged?.win_rate;
    cells.weekWinRate.textContent =
      rate != null && Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : "—";
  }
  if (cells.weekNet) {
    const net = merged?.net_pnl_gbp;
    if (net != null && Number.isFinite(net)) {
      cells.weekNet.textContent = `${net >= 0 ? "+" : "−"}£${Math.abs(net).toFixed(2)}`;
      cells.weekNet.dataset.tone = net >= 0 ? "profit" : net < 0 ? "loss" : "mute";
    } else {
      cells.weekNet.textContent = "—";
      cells.weekNet.dataset.tone = "mute";
    }
  }
  if (cells.weekAssets) {
    cells.weekAssets.textContent = formatAssetBreakdown(weekly?.asset_breakdown, 4);
  }

  const accounts = weekly?.accounts ?? {};
  const cfd = accounts.Z6BAH4;
  const sb = accounts.Z6BAH3;
  if (cells.weekCfd) {
    cells.weekCfd.textContent = cfd
      ? `CFD ${((cfd.win_rate ?? 0) * 100).toFixed(0)}% · PF ${fmt(cfd.asymmetric_profit_factor, 2)} · ${cfd.wins ?? 0}W/${cfd.losses ?? 0}L`
      : "CFD —";
  }
  if (cells.weekSb) {
    cells.weekSb.textContent = sb
      ? `SB ${((sb.win_rate ?? 0) * 100).toFixed(0)}% · PF ${fmt(sb.asymmetric_profit_factor, 2)} · ${sb.wins ?? 0}W/${sb.losses ?? 0}L`
      : "SB —";
  }
}

export function PerformanceMetricsBlotter() {
  const cellsRef = useRef<CellRefs>({
    scope: null,
    sharpe: null,
    profitFactor: null,
    winLoss: null,
    winRate: null,
    dayNet: null,
    weekScope: null,
    weekSharpe: null,
    weekPf: null,
    weekWinRate: null,
    weekNet: null,
    weekAssets: null,
    weekCfd: null,
    weekSb: null,
  });
  const lastPerfRef = useRef<PerfMetrics>({});
  const lastWeeklyRef = useRef<WeeklyMetricsPayload | null>(null);

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
          lastPerfRef.current = merged.performance_metrics || {};
          paintMetrics(
            cellsRef.current,
            lastPerfRef.current,
            merged.today_net_realized_pnl_gbp ?? null,
          );
        }
        const weekly = mergeWeeklyMetrics(
          cfdWeek.ok ? cfdWeek.payload : null,
          sbWeek.ok ? sbWeek.payload : null,
        );
        lastWeeklyRef.current = weekly;
        paintWeekly(cellsRef.current, weekly);
      } catch {
        /* keep last painted values */
      }
    };

    void pull();
    const id = window.setInterval(pull, 8000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const bind =
    (key: keyof CellRefs) =>
    (el: HTMLSpanElement | null) => {
      cellsRef.current[key] = el;
      if (!el) return;
      if (Object.keys(lastPerfRef.current).length > 0) {
        paintMetrics(cellsRef.current, lastPerfRef.current, null);
      }
      if (lastWeeklyRef.current) {
        paintWeekly(cellsRef.current, lastWeeklyRef.current);
      }
    };

  return (
    <section
      className="perf-metrics-blotter"
      aria-label="Intra-day and weekly performance metrics blotter"
    >
      <div className="perf-metrics-head">
        <p className="gpu-kicker">Performance · Today &amp; week</p>
        <span ref={bind("scope")} className="gpu-chip gpu-chip--mono">
          TODAY · n=0
        </span>
      </div>
      <div className="perf-metrics-grid">
        <div className="perf-metrics-cell">
          <span className="gpu-metric-key">INTRA-DAY SHARPE</span>
          <span ref={bind("sharpe")} className="perf-metrics-value gpu-ledger-mono">
            —
          </span>
        </div>
        <div className="perf-metrics-cell">
          <span className="gpu-metric-key">PROFIT FACTOR</span>
          <span ref={bind("profitFactor")} className="perf-metrics-value gpu-ledger-mono">
            —
          </span>
        </div>
        <div className="perf-metrics-cell">
          <span className="gpu-metric-key">WIN / LOSS</span>
          <span ref={bind("winLoss")} className="perf-metrics-value gpu-ledger-mono">
            —
            <span className="perf-metrics-sub"> · —</span>
          </span>
        </div>
        <div className="perf-metrics-cell">
          <span className="gpu-metric-key">WIN RATE</span>
          <span ref={bind("winRate")} className="perf-metrics-value gpu-ledger-mono">
            —
          </span>
        </div>
        <div className="perf-metrics-cell">
          <span className="gpu-metric-key">TODAY&apos;S NET P&amp;L</span>
          <span ref={bind("dayNet")} className="perf-metrics-value gpu-ledger-mono" data-tone="mute">
            —
          </span>
        </div>
      </div>

      <div className="perf-metrics-weekly">
        <div className="perf-metrics-head perf-metrics-head--weekly">
          <p className="gpu-kicker gpu-kicker--dim">Weekly P&amp;L</p>
          <span ref={bind("weekScope")} className="gpu-chip gpu-chip--mono">
            7D · n=0
          </span>
        </div>
        <div className="perf-metrics-grid perf-metrics-grid--weekly">
          <div className="perf-metrics-cell">
            <span className="gpu-metric-key">WEEKLY SHARPE</span>
            <span ref={bind("weekSharpe")} className="perf-metrics-value gpu-ledger-mono">
              —
            </span>
          </div>
          <div className="perf-metrics-cell">
            <span className="gpu-metric-key">ASYMMETRIC PF</span>
            <span ref={bind("weekPf")} className="perf-metrics-value gpu-ledger-mono">
              —
            </span>
          </div>
          <div className="perf-metrics-cell">
            <span className="gpu-metric-key">WEEK WIN RATE</span>
            <span ref={bind("weekWinRate")} className="perf-metrics-value gpu-ledger-mono">
              —
            </span>
          </div>
          <div className="perf-metrics-cell">
            <span className="gpu-metric-key">WEEK NET GBP</span>
            <span ref={bind("weekNet")} className="perf-metrics-value gpu-ledger-mono" data-tone="mute">
              —
            </span>
          </div>
          <div className="perf-metrics-cell perf-metrics-cell--wide">
            <span className="gpu-metric-key">ASSET BREAKDOWN</span>
            <span ref={bind("weekAssets")} className="perf-metrics-value perf-metrics-value--compact gpu-ledger-mono">
              —
            </span>
          </div>
        </div>
        <div className="perf-metrics-accounts">
          <span ref={bind("weekCfd")} className="gpu-chip gpu-chip--mono perf-metrics-account-chip">
            CFD —
          </span>
          <span ref={bind("weekSb")} className="gpu-chip gpu-chip--mono perf-metrics-account-chip">
            SB —
          </span>
        </div>
      </div>
    </section>
  );
}
