/**
 * Merge simplified accounting from CFD :8080 and SB :8081 into one sovereign ledger.
 */

export type ClosedTradeRow = {
  timestamp: string;
  asset: string;
  direction: string;
  net_pnl_gbp: number;
  result?: string;
  engine_origin?: string;
  engine_label?: string;
  account_id?: string;
  product_type?: string;
};

export type DailyHistoryRow = {
  date: string;
  pnl_gbp: number;
};

export type PerformanceMetrics = {
  intraday_sharpe?: number;
  profit_factor?: number;
  win_loss_ratio?: number;
  win_rate?: number;
  wins?: number;
  losses?: number;
  true_wins?: number;
  true_losses?: number;
  true_win_rate?: number;
  true_win_loss_ratio?: number;
  gross_wins_gbp?: number;
  gross_losses_gbp?: number;
  net_true_outcome_gbp?: number;
  breakeven_excluded?: number;
  sample_n?: number;
  raw_sample_n?: number;
  sample_scope?: string;
};

export type SimplifiedAccountingPayload = {
  ok: boolean;
  source?: string;
  empty_day?: boolean;
  today_net_realized_pnl_gbp: number;
  last_10_closed_trades: ClosedTradeRow[];
  daily_history: DailyHistoryRow[];
  performance_metrics?: PerformanceMetrics;
  system_state?: Record<string, unknown>;
};

const ENGINE_TAGS = {
  cfd: { origin: "QUANT_SNIPER", label: "CFD" },
  sb: { origin: "MACRO_SENTINEL", label: "SB" },
} as const;

function tagTrades(
  rows: ClosedTradeRow[] | undefined,
  port: keyof typeof ENGINE_TAGS,
): ClosedTradeRow[] {
  const tag = ENGINE_TAGS[port];
  return (rows ?? []).map((r) => ({
    ...r,
    engine_origin: tag.origin,
    engine_label: tag.label,
  }));
}

export function mergeSimplifiedAccounting(
  cfd: SimplifiedAccountingPayload | null,
  sb: SimplifiedAccountingPayload | null,
): SimplifiedAccountingPayload | null {
  if (!cfd && !sb) return null;
  if (!cfd) return sb;
  if (!sb) return cfd;

  const today =
    (cfd.today_net_realized_pnl_gbp ?? 0) + (sb.today_net_realized_pnl_gbp ?? 0);

  const mergedTrades = [
    ...tagTrades(cfd.last_10_closed_trades, "cfd"),
    ...tagTrades(sb.last_10_closed_trades, "sb"),
  ]
    .sort((a, b) => String(b.timestamp || "").localeCompare(String(a.timestamp || "")))
    .slice(0, 10);

  const dayMap = new Map<string, number>();
  for (const payload of [cfd, sb]) {
    for (const row of payload.daily_history ?? []) {
      const d = String(row.date || "");
      if (!d) continue;
      dayMap.set(d, (dayMap.get(d) ?? 0) + Number(row.pnl_gbp ?? 0));
    }
  }
  const daily_history = [...dayMap.entries()]
    .map(([date, pnl_gbp]) => ({ date, pnl_gbp: Math.round(pnl_gbp * 10000) / 10000 }))
    .sort((a, b) => b.date.localeCompare(a.date));

  const source =
    cfd.source && sb.source && cfd.source !== sb.source
      ? "dual_merged"
      : cfd.source || sb.source;

  const system_state = {
    ...(cfd.system_state || {}),
    ...(sb.system_state || {}),
    dual_merged: true,
  };

  return {
    ok: Boolean(cfd.ok || sb.ok),
    source,
    empty_day: Math.abs(today) < 1e-9 && mergedTrades.length === 0,
    today_net_realized_pnl_gbp: today,
    last_10_closed_trades: mergedTrades,
    daily_history,
    performance_metrics: mergePerformanceMetrics(
      cfd.performance_metrics,
      sb.performance_metrics,
    ),
    system_state,
  };
}

function mergePerformanceMetrics(
  cfd: PerformanceMetrics | undefined,
  sb: PerformanceMetrics | undefined,
): PerformanceMetrics | undefined {
  if (!cfd && !sb) return undefined;
  if (!cfd) return sb;
  if (!sb) return cfd;

  const wins = Number(cfd.true_wins ?? cfd.wins ?? 0) + Number(sb.true_wins ?? sb.wins ?? 0);
  const losses =
    Number(cfd.true_losses ?? cfd.losses ?? 0) + Number(sb.true_losses ?? sb.losses ?? 0);
  const grossWins =
    Number(cfd.gross_wins_gbp ?? 0) + Number(sb.gross_wins_gbp ?? 0);
  const grossLosses =
    Number(cfd.gross_losses_gbp ?? 0) + Number(sb.gross_losses_gbp ?? 0);
  const n = wins + losses;
  const breakevenExcluded =
    Number(cfd.breakeven_excluded ?? 0) + Number(sb.breakeven_excluded ?? 0);
  const profitFactor =
    grossLosses > 1e-12
      ? grossWins / grossLosses
      : grossWins > 0
        ? 99
        : 0;
  const winLossRatio = losses > 0 ? wins / losses : wins > 0 ? wins : 0;
  const winRate = n > 0 ? wins / n : 0;
  const sharpeValues = [cfd.intraday_sharpe, sb.intraday_sharpe].filter(
    (v): v is number => v != null && Number.isFinite(v),
  );
  const intradaySharpe =
    sharpeValues.length === 0
      ? 0
      : sharpeValues.reduce((a, b) => a + b, 0) / sharpeValues.length;

  return {
    intraday_sharpe: Math.round(intradaySharpe * 10000) / 10000,
    profit_factor: Math.round(profitFactor * 10000) / 10000,
    win_loss_ratio: Math.round(winLossRatio * 10000) / 10000,
    win_rate: Math.round(winRate * 10000) / 10000,
    wins,
    losses,
    true_wins: wins,
    true_losses: losses,
    true_win_rate: Math.round(winRate * 10000) / 10000,
    true_win_loss_ratio: Math.round(winLossRatio * 10000) / 10000,
    gross_wins_gbp: Math.round(grossWins * 10000) / 10000,
    gross_losses_gbp: Math.round(grossLosses * 10000) / 10000,
    net_true_outcome_gbp: Math.round((grossWins - grossLosses) * 10000) / 10000,
    breakeven_excluded: breakevenExcluded,
    sample_n: n,
    raw_sample_n:
      Number(cfd.raw_sample_n ?? cfd.sample_n ?? 0) +
      Number(sb.raw_sample_n ?? sb.sample_n ?? 0),
    sample_scope: "dual_merged",
  };
}
