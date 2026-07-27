/**
 * Merge weekly metrics from CFD :8080 and SB :8081 by account_id.
 *
 * Both ports currently compile from the same shared journal and return the
 * full multi-account weekly block. Summing `merged.net_pnl_gbp` (or summing
 * the same AccountID twice) double-counts. Deduplicate shared clones / same
 * account_id rows; roll up combined PnL from distinct accounts only.
 */

import { nearlyEqualGbp } from "./desk-accounting-merge";

export type AssetBreakdownRow = {
  asset: string;
  pnl_gbp: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
};

export type WeeklyMetricsBlock = {
  weekly_sharpe: number | null;
  asymmetric_profit_factor: number;
  win_rate: number;
  wins: number;
  losses: number;
  gross_wins_gbp?: number;
  gross_losses_gbp?: number;
  net_pnl_gbp: number;
  sample_n: number;
  trading_days?: number;
  asset_breakdown?: AssetBreakdownRow[];
};

export type WeeklyAccountMetrics = WeeklyMetricsBlock & {
  account_id: string;
  product_type: string;
  engine_origin: string;
};

export type WeeklyMetricsPayload = {
  ok: boolean;
  source?: string;
  week_start?: string;
  week_end?: string;
  merged: WeeklyMetricsBlock;
  accounts: Record<string, WeeklyAccountMetrics>;
  asset_breakdown: AssetBreakdownRow[];
  cache_age_sec?: number;
  dual_merged?: boolean;
  shared_journal_deduped?: boolean;
};

function emptyBlock(): WeeklyMetricsBlock {
  return {
    weekly_sharpe: null,
    asymmetric_profit_factor: 0,
    win_rate: 0,
    wins: 0,
    losses: 0,
    gross_wins_gbp: 0,
    gross_losses_gbp: 0,
    net_pnl_gbp: 0,
    sample_n: 0,
    trading_days: 0,
    asset_breakdown: [],
  };
}

export function isSharedWeeklyClone(
  cfd: WeeklyMetricsPayload,
  sb: WeeklyMetricsPayload,
): boolean {
  const a = Number(cfd.merged?.net_pnl_gbp ?? 0);
  const b = Number(sb.merged?.net_pnl_gbp ?? 0);
  if (!nearlyEqualGbp(a, b)) return false;

  const srcC = String(cfd.source || "");
  const srcS = String(sb.source || "");
  const journalLike =
    srcC.length > 0 &&
    srcC === srcS &&
    (srcC.includes("journal") || srcC === "learning_db");

  const keysC = Object.keys(cfd.accounts ?? {}).sort().join(",");
  const keysS = Object.keys(sb.accounts ?? {}).sort().join(",");
  const sameAccountSet = keysC.length > 0 && keysC === keysS;

  return journalLike || sameAccountSet;
}

function mergeAssetRows(
  cfd: AssetBreakdownRow[] | undefined,
  sb: AssetBreakdownRow[] | undefined,
  sharedClone: boolean,
): AssetBreakdownRow[] {
  if (sharedClone) {
    const preferred = (cfd?.length ?? 0) >= (sb?.length ?? 0) ? cfd : sb;
    return [...(preferred ?? [])];
  }

  const map = new Map<string, AssetBreakdownRow>();
  for (const row of [...(cfd ?? []), ...(sb ?? [])]) {
    const asset = String(row.asset || "—");
    const prev = map.get(asset);
    if (!prev) {
      map.set(asset, { ...row, asset });
      continue;
    }
    // Identical asset row from both ports → shared journal, keep one.
    if (
      nearlyEqualGbp(prev.pnl_gbp, Number(row.pnl_gbp ?? 0)) &&
      Number(prev.trades ?? 0) === Number(row.trades ?? 0)
    ) {
      continue;
    }
    const trades = prev.trades + Number(row.trades ?? 0);
    const wins = prev.wins + Number(row.wins ?? 0);
    const losses = prev.losses + Number(row.losses ?? 0);
    map.set(asset, {
      asset,
      pnl_gbp: Math.round((prev.pnl_gbp + Number(row.pnl_gbp ?? 0)) * 10000) / 10000,
      trades,
      wins,
      losses,
      win_rate: trades > 0 ? Math.round((wins / trades) * 10000) / 10000 : 0,
    });
  }
  return [...map.values()].sort((a, b) => Math.abs(b.pnl_gbp) - Math.abs(a.pnl_gbp));
}

function mergeMetricsBlock(
  cfd: WeeklyMetricsBlock | undefined,
  sb: WeeklyMetricsBlock | undefined,
  sharedClone = false,
): WeeklyMetricsBlock {
  if (!cfd && !sb) return emptyBlock();
  if (!cfd) return { ...sb!, asset_breakdown: sb!.asset_breakdown ?? [] };
  if (!sb) return { ...cfd, asset_breakdown: cfd.asset_breakdown ?? [] };
  if (sharedClone || nearlyEqualGbp(cfd.net_pnl_gbp, sb.net_pnl_gbp)) {
    const preferred =
      Number(cfd.sample_n ?? 0) >= Number(sb.sample_n ?? 0) ? cfd : sb;
    return {
      ...preferred,
      asset_breakdown: preferred.asset_breakdown ?? [],
    };
  }

  const wins = Number(cfd.wins ?? 0) + Number(sb.wins ?? 0);
  const losses = Number(cfd.losses ?? 0) + Number(sb.losses ?? 0);
  const grossWins = Number(cfd.gross_wins_gbp ?? 0) + Number(sb.gross_wins_gbp ?? 0);
  const grossLosses = Number(cfd.gross_losses_gbp ?? 0) + Number(sb.gross_losses_gbp ?? 0);
  const n = wins + losses;
  const profitFactor =
    grossLosses > 1e-12 ? grossWins / grossLosses : grossWins > 0 ? 99 : 0;
  const sharpeValues = [cfd.weekly_sharpe, sb.weekly_sharpe].filter(
    (v): v is number => v != null && Number.isFinite(v),
  );
  const weeklySharpe =
    sharpeValues.length === 0
      ? null
      : Math.round((sharpeValues.reduce((a, b) => a + b, 0) / sharpeValues.length) * 10000) /
        10000;

  return {
    weekly_sharpe: weeklySharpe,
    asymmetric_profit_factor: Math.round(profitFactor * 10000) / 10000,
    win_rate: n > 0 ? Math.round((wins / n) * 10000) / 10000 : 0,
    wins,
    losses,
    gross_wins_gbp: Math.round(grossWins * 10000) / 10000,
    gross_losses_gbp: Math.round(grossLosses * 10000) / 10000,
    net_pnl_gbp: Math.round((grossWins - grossLosses) * 10000) / 10000,
    sample_n: n,
    trading_days: Math.max(Number(cfd.trading_days ?? 0), Number(sb.trading_days ?? 0)),
    asset_breakdown: mergeAssetRows(cfd.asset_breakdown, sb.asset_breakdown, false),
  };
}

function rollupFromAccounts(accounts: WeeklyAccountMetrics[]): WeeklyMetricsBlock {
  if (accounts.length === 0) return emptyBlock();
  if (accounts.length === 1) {
    const only = accounts[0];
    return {
      ...only,
      asset_breakdown: only.asset_breakdown ?? [],
    };
  }

  let wins = 0;
  let losses = 0;
  let grossWins = 0;
  let grossLosses = 0;
  let tradingDays = 0;
  const sharpeValues: number[] = [];
  let assets: AssetBreakdownRow[] = [];

  for (const acct of accounts) {
    wins += Number(acct.wins ?? 0);
    losses += Number(acct.losses ?? 0);
    grossWins += Number(acct.gross_wins_gbp ?? 0);
    grossLosses += Number(acct.gross_losses_gbp ?? 0);
    tradingDays = Math.max(tradingDays, Number(acct.trading_days ?? 0));
    if (acct.weekly_sharpe != null && Number.isFinite(acct.weekly_sharpe)) {
      sharpeValues.push(acct.weekly_sharpe);
    }
    assets = mergeAssetRows(assets, acct.asset_breakdown, false);
  }

  const n = wins + losses;
  const profitFactor =
    grossLosses > 1e-12 ? grossWins / grossLosses : grossWins > 0 ? 99 : 0;
  const weeklySharpe =
    sharpeValues.length === 0
      ? null
      : Math.round((sharpeValues.reduce((a, b) => a + b, 0) / sharpeValues.length) * 10000) /
        10000;

  return {
    weekly_sharpe: weeklySharpe,
    asymmetric_profit_factor: Math.round(profitFactor * 10000) / 10000,
    win_rate: n > 0 ? Math.round((wins / n) * 10000) / 10000 : 0,
    wins,
    losses,
    gross_wins_gbp: Math.round(grossWins * 10000) / 10000,
    gross_losses_gbp: Math.round(grossLosses * 10000) / 10000,
    net_pnl_gbp: Math.round((grossWins - grossLosses) * 10000) / 10000,
    sample_n: n,
    trading_days: tradingDays,
    asset_breakdown: assets,
  };
}

function mergeAccounts(
  cfdAccounts: Record<string, WeeklyAccountMetrics> | undefined,
  sbAccounts: Record<string, WeeklyAccountMetrics> | undefined,
): Record<string, WeeklyAccountMetrics> {
  const out: Record<string, WeeklyAccountMetrics> = {};
  const keys = new Set([
    ...Object.keys(cfdAccounts ?? {}),
    ...Object.keys(sbAccounts ?? {}),
  ]);
  for (const key of keys) {
    const cfd = cfdAccounts?.[key];
    const sb = sbAccounts?.[key];
    if (cfd && !sb) {
      out[key] = cfd;
      continue;
    }
    if (sb && !cfd) {
      out[key] = sb;
      continue;
    }
    // Same AccountID from both ports with matching PnL → one contribution.
    if (
      nearlyEqualGbp(Number(cfd!.net_pnl_gbp ?? 0), Number(sb!.net_pnl_gbp ?? 0)) &&
      Number(cfd!.wins ?? 0) === Number(sb!.wins ?? 0) &&
      Number(cfd!.losses ?? 0) === Number(sb!.losses ?? 0)
    ) {
      out[key] =
        Number(cfd!.sample_n ?? 0) >= Number(sb!.sample_n ?? 0) ? cfd! : sb!;
      continue;
    }
    const block = mergeMetricsBlock(cfd, sb, false);
    out[key] = {
      account_id: key,
      product_type: cfd?.product_type || sb?.product_type || "",
      engine_origin: cfd?.engine_origin || sb?.engine_origin || "",
      ...block,
      asset_breakdown: mergeAssetRows(cfd?.asset_breakdown, sb?.asset_breakdown, false),
    };
  }
  return out;
}

export function mergeWeeklyMetrics(
  cfd: WeeklyMetricsPayload | null,
  sb: WeeklyMetricsPayload | null,
): WeeklyMetricsPayload | null {
  if (!cfd && !sb) return null;
  if (!cfd) return sb;
  if (!sb) return cfd;

  const sharedClone = isSharedWeeklyClone(cfd, sb);
  if (sharedClone) {
    const preferred =
      Object.keys(cfd.accounts ?? {}).length >= Object.keys(sb.accounts ?? {}).length
        ? cfd
        : sb;
    return {
      ...preferred,
      dual_merged: false,
      shared_journal_deduped: true,
      cache_age_sec: Math.max(Number(cfd.cache_age_sec ?? 0), Number(sb.cache_age_sec ?? 0)),
    };
  }

  const accounts = mergeAccounts(cfd.accounts, sb.accounts);
  const accountList = Object.values(accounts);
  // Prefer rollup of distinct accounts over summing two global merged blocks.
  const merged =
    accountList.length > 0
      ? rollupFromAccounts(accountList)
      : mergeMetricsBlock(cfd.merged, sb.merged, false);

  return {
    ok: Boolean(cfd.ok || sb.ok),
    source:
      cfd.source && sb.source && cfd.source !== sb.source
        ? "dual_merged"
        : cfd.source || sb.source,
    week_start: cfd.week_start || sb.week_start,
    week_end: cfd.week_end || sb.week_end,
    merged,
    accounts,
    asset_breakdown: merged.asset_breakdown ?? [],
    cache_age_sec: Math.max(Number(cfd.cache_age_sec ?? 0), Number(sb.cache_age_sec ?? 0)),
    dual_merged: true,
    shared_journal_deduped: false,
  };
}

export function formatAssetBreakdown(rows: AssetBreakdownRow[] | undefined, limit = 4): string {
  const list = (rows ?? []).slice(0, limit);
  if (list.length === 0) return "—";
  return list
    .map((r) => {
      const sign = r.pnl_gbp >= 0 ? "+" : "−";
      return `${r.asset} ${sign}£${Math.abs(r.pnl_gbp).toFixed(0)} (${r.trades})`;
    })
    .join(" · ");
}
