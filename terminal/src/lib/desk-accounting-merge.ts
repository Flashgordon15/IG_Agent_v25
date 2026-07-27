/**
 * Merge simplified accounting from CFD :8080 and SB :8081 into one sovereign ledger.
 *
 * Both ports currently read the same unfiltered daily_journal.csv, so a naive
 * sum of today_net_realized_pnl_gbp double-counts one shared journal day
 * (forensics: −£258.91 × 2 ≈ −£517 GUI artifact). Deduplicate shared global
 * aggregates; only sum when the two payloads look like distinct contributions.
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
  epic?: string;
  deal_id?: string;
};

/** Local journal row (Next `/api/desk/journal`) — enriches agent blotter labels. */
export type JournalEnrichRow = {
  timestamp?: string;
  dealId?: string;
  direction?: string;
  realizedGbp?: number | null;
  accountId?: string;
  productType?: string;
  engineOrigin?: string;
  market?: string;
  epic?: string;
};

const ACCOUNT_PRODUCT: Record<string, { product: string; label: string }> = {
  Z6BAH4: { product: "CFD", label: "CFD" },
  Z6BAH3: { product: "SPREADBET", label: "SB" },
};

const EPIC_ASSET: Record<string, string> = {
  "IX.D.DOW.IFM.IP": "DOW",
  "IX.D.NIKKEI.IFM.IP": "NIKKEI",
  "CS.D.CFPGOLD.CFP.IP": "GOLD",
  "CS.D.EURUSD.CFD.IP": "EURUSD",
  "IX.D.FTSE.IFM.IP": "FTSE",
};

function isPlaceholderAccount(v: string | undefined): boolean {
  const s = String(v || "").trim().toUpperCase();
  return !s || s === "SHARED" || s === "—" || s === "-";
}

function isPlaceholderProduct(v: string | undefined): boolean {
  const s = String(v || "").trim().toUpperCase();
  return !s || s === "JOURNAL" || s === "SHARED" || s === "—" || s === "-";
}

function assetFromEpic(epic: string | undefined, market?: string): string {
  const e = String(epic || "").trim();
  if (e && EPIC_ASSET[e]) return EPIC_ASSET[e];
  const m = String(market || "").toLowerCase();
  if (m.includes("dow") || m.includes("wall street")) return "DOW";
  if (m.includes("nikkei") || m.includes("japan")) return "NIKKEI";
  if (m.includes("gold")) return "GOLD";
  if (m.includes("eur") && m.includes("usd")) return "EURUSD";
  if (m.includes("ftse")) return "FTSE";
  if (e) {
    const parts = e.split(".");
    return parts.length >= 3 ? parts[2] : e.slice(0, 18);
  }
  return "";
}

function looksLikeDealId(asset: string | undefined): boolean {
  const a = String(asset || "").trim();
  return a.startsWith("DI") || /^[A-Z0-9]{8,}$/i.test(a);
}

/** Map deal id / suffix → journal enrich row. */
export function buildJournalEnrichIndex(
  rows: JournalEnrichRow[] | undefined,
): Map<string, JournalEnrichRow> {
  const out = new Map<string, JournalEnrichRow>();
  for (const row of rows ?? []) {
    const deal = String(row.dealId || "").trim();
    if (!deal) continue;
    out.set(deal, row);
    if (deal.length >= 8) out.set(deal.slice(-8), row);
  }
  return out;
}

/**
 * Overlay AccountID / ProductType / human asset from local journal (and optional
 * learning closed-ledger) onto agent simplified_accounting rows.
 */
export function enrichClosedTradesFromJournal(
  trades: ClosedTradeRow[],
  journalRows?: JournalEnrichRow[],
  ledgerRows?: JournalEnrichRow[],
): ClosedTradeRow[] {
  const journalIdx = buildJournalEnrichIndex(journalRows);
  const ledgerIdx = buildJournalEnrichIndex(ledgerRows);

  return trades.map((row) => {
    const deal =
      String(row.deal_id || "").trim() ||
      (looksLikeDealId(row.asset) ? String(row.asset).trim() : "");
    const jHit =
      (deal && (journalIdx.get(deal) || journalIdx.get(deal.slice(-8)))) || undefined;
    const lHit =
      (deal && (ledgerIdx.get(deal) || ledgerIdx.get(deal.slice(-8)))) || undefined;

    let account_id = row.account_id;
    let product_type = row.product_type;
    let engine_origin = row.engine_origin;
    let epic = row.epic;
    let asset = row.asset;

    if (jHit) {
      if (isPlaceholderAccount(account_id) && jHit.accountId) {
        account_id = jHit.accountId;
      }
      if (isPlaceholderProduct(product_type) && jHit.productType) {
        product_type = String(jHit.productType).toUpperCase();
      }
      if (!engine_origin && jHit.engineOrigin) {
        engine_origin = jHit.engineOrigin;
      }
      if (!epic && jHit.epic) {
        epic = jHit.epic;
      }
    }
    if (lHit) {
      if (!epic && lHit.epic) epic = lHit.epic;
      if (isPlaceholderAccount(account_id) && lHit.accountId) {
        account_id = lHit.accountId;
      }
      if (isPlaceholderProduct(product_type) && lHit.productType) {
        product_type = String(lHit.productType).toUpperCase();
      }
    }

    const human = assetFromEpic(epic, lHit?.market || jHit?.market);
    if (human && (looksLikeDealId(asset) || !asset || asset === "—")) {
      asset = human;
    }

    const acctKey = String(account_id || "").trim().toUpperCase();
    const derived = ACCOUNT_PRODUCT[acctKey];
    if (derived && isPlaceholderProduct(product_type)) {
      product_type = derived.product;
    }

    let engine_label = row.engine_label;
    if (!engine_label || engine_label === "SHARED" || engine_label === "JOURNAL") {
      if (derived) engine_label = derived.label;
      else if (String(product_type || "").toUpperCase() === "CFD") engine_label = "CFD";
      else if (String(product_type || "").toUpperCase() === "SPREADBET")
        engine_label = "SB";
    }

    return {
      ...row,
      asset,
      account_id: isPlaceholderAccount(account_id) ? undefined : account_id,
      product_type: isPlaceholderProduct(product_type) ? undefined : product_type,
      engine_origin,
      engine_label,
      epic,
      deal_id: deal || row.deal_id,
    };
  });
}

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

/** Penny-level match — shared journal clones agree to 0.01–0.02 GBP. */
export function nearlyEqualGbp(a: number, b: number, eps = 0.02): boolean {
  return Math.abs(Number(a) - Number(b)) <= eps;
}

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

function tradeFingerprint(row: ClosedTradeRow): string {
  return [
    String(row.timestamp || ""),
    String(row.asset || ""),
    String(row.direction || ""),
    Number(row.net_pnl_gbp ?? 0).toFixed(4),
  ].join("|");
}

function dedupeTrades(rows: ClosedTradeRow[]): ClosedTradeRow[] {
  const seen = new Set<string>();
  const out: ClosedTradeRow[] = [];
  for (const row of rows) {
    const key = tradeFingerprint(row);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(row);
  }
  return out;
}

/**
 * True when both ports expose the same unfiltered global journal aggregate
 * (identical today total + journal-like source, and/or matching day history).
 */
export function isSharedGlobalJournalClone(
  cfd: SimplifiedAccountingPayload,
  sb: SimplifiedAccountingPayload,
): boolean {
  const cfdToday = Number(cfd.today_net_realized_pnl_gbp ?? 0);
  const sbToday = Number(sb.today_net_realized_pnl_gbp ?? 0);
  if (!nearlyEqualGbp(cfdToday, sbToday)) return false;

  const srcC = String(cfd.source || "");
  const srcS = String(sb.source || "");
  const journalLike =
    srcC.length > 0 &&
    srcC === srcS &&
    (srcC.includes("journal") || srcC === "learning_db" || srcC === "ig_ledger");

  const cfdDays = cfd.daily_history ?? [];
  const sbDays = sb.daily_history ?? [];
  let matchingDays = 0;
  let comparedDays = 0;
  if (cfdDays.length > 0 && sbDays.length > 0) {
    const sbMap = new Map(sbDays.map((r) => [String(r.date), Number(r.pnl_gbp ?? 0)]));
    for (const row of cfdDays.slice(0, 5)) {
      const d = String(row.date || "");
      if (!d || !sbMap.has(d)) continue;
      comparedDays += 1;
      if (nearlyEqualGbp(Number(row.pnl_gbp ?? 0), sbMap.get(d) ?? 0)) matchingDays += 1;
    }
  }

  const historyClone = comparedDays >= 1 && matchingDays === comparedDays;
  const perfClone =
    cfd.performance_metrics != null &&
    sb.performance_metrics != null &&
    nearlyEqualGbp(
      Number(cfd.performance_metrics.net_true_outcome_gbp ?? cfdToday),
      Number(sb.performance_metrics.net_true_outcome_gbp ?? sbToday),
    ) &&
    Number(cfd.performance_metrics.sample_n ?? -1) ===
      Number(sb.performance_metrics.sample_n ?? -2);

  return journalLike || historyClone || perfClone;
}

function mergeDailyHistory(
  cfd: DailyHistoryRow[] | undefined,
  sb: DailyHistoryRow[] | undefined,
  sharedClone: boolean,
): DailyHistoryRow[] {
  if (sharedClone) {
    const preferred = (cfd?.length ?? 0) >= (sb?.length ?? 0) ? cfd : sb;
    return [...(preferred ?? [])].sort((a, b) => b.date.localeCompare(a.date));
  }

  const dayMap = new Map<string, number>();
  const seenExact = new Map<string, number>();
  for (const payload of [cfd ?? [], sb ?? []]) {
    for (const row of payload) {
      const d = String(row.date || "");
      if (!d) continue;
      const pnl = Number(row.pnl_gbp ?? 0);
      if (!dayMap.has(d)) {
        dayMap.set(d, pnl);
        seenExact.set(d, pnl);
        continue;
      }
      const prior = dayMap.get(d) ?? 0;
      // Same day, same figure from both ports → shared history, do not sum.
      if (nearlyEqualGbp(prior, pnl) && nearlyEqualGbp(seenExact.get(d) ?? prior, pnl)) {
        continue;
      }
      dayMap.set(d, prior + pnl);
    }
  }
  return [...dayMap.entries()]
    .map(([date, pnl_gbp]) => ({ date, pnl_gbp: Math.round(pnl_gbp * 10000) / 10000 }))
    .sort((a, b) => b.date.localeCompare(a.date));
}

function combineTodayNet(
  cfd: SimplifiedAccountingPayload,
  sb: SimplifiedAccountingPayload,
  sharedClone: boolean,
): number {
  const a = Number(cfd.today_net_realized_pnl_gbp ?? 0);
  const b = Number(sb.today_net_realized_pnl_gbp ?? 0);
  if (sharedClone || nearlyEqualGbp(a, b)) {
    return Math.round(a * 10000) / 10000;
  }
  return Math.round((a + b) * 10000) / 10000;
}

export function mergeSimplifiedAccounting(
  cfd: SimplifiedAccountingPayload | null,
  sb: SimplifiedAccountingPayload | null,
): SimplifiedAccountingPayload | null {
  if (!cfd && !sb) return null;
  if (!cfd) return sb;
  if (!sb) return cfd;

  const sharedClone = isSharedGlobalJournalClone(cfd, sb);
  const today = combineTodayNet(cfd, sb, sharedClone);
  // Near-identical today totals ⇒ treat blotter as one journal even if clone
  // heuristics miss (avoids CFD+SB mirrored pairs in the closed-trade table).
  const blotterShared =
    sharedClone ||
    nearlyEqualGbp(
      Number(cfd.today_net_realized_pnl_gbp ?? 0),
      Number(sb.today_net_realized_pnl_gbp ?? 0),
    );

  const labelSharedRow = (r: ClosedTradeRow): ClosedTradeRow => {
    // Keep real journal AccountID / ProductType. Only fall back to SHARED as a
    // *source chip* via system_state — never paint every row JOURNAL/SHARED.
    const acct = String(r.account_id || "").trim();
    const prod = String(r.product_type || "").trim().toUpperCase();
    const derived = ACCOUNT_PRODUCT[acct.toUpperCase()];
    const product_type =
      prod && prod !== "JOURNAL" && prod !== "SHARED"
        ? prod
        : derived?.product || prod || undefined;
    const account_id = acct && acct.toUpperCase() !== "SHARED" ? acct : acct || undefined;
    let engine_label = r.engine_label;
    if (!engine_label || engine_label === "SHARED" || engine_label === "JOURNAL") {
      engine_label = derived?.label
        || (product_type === "CFD" ? "CFD" : product_type === "SPREADBET" ? "SB" : undefined);
    }
    return {
      ...r,
      account_id,
      product_type,
      engine_label,
    };
  };

  const mergedTrades = blotterShared
    ? dedupeTrades([
        ...(cfd.last_10_closed_trades ?? []),
        ...(sb.last_10_closed_trades ?? []),
      ])
        .map(labelSharedRow)
        .sort((a, b) => String(b.timestamp || "").localeCompare(String(a.timestamp || "")))
        .slice(0, 10)
    : dedupeTrades([
        ...tagTrades(cfd.last_10_closed_trades, "cfd"),
        ...tagTrades(sb.last_10_closed_trades, "sb"),
      ])
        .sort((a, b) => String(b.timestamp || "").localeCompare(String(a.timestamp || "")))
        .slice(0, 10);

  const daily_history = mergeDailyHistory(cfd.daily_history, sb.daily_history, sharedClone);

  const source = sharedClone
    ? cfd.source || sb.source || "shared_journal_once"
    : cfd.source && sb.source && cfd.source !== sb.source
      ? "dual_merged"
      : cfd.source || sb.source;

  const system_state = {
    ...(cfd.system_state || {}),
    ...(sb.system_state || {}),
    dual_merged: !sharedClone,
    shared_journal_deduped: sharedClone,
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
      sharedClone,
    ),
    system_state,
  };
}

function mergePerformanceMetrics(
  cfd: PerformanceMetrics | undefined,
  sb: PerformanceMetrics | undefined,
  sharedClone: boolean,
): PerformanceMetrics | undefined {
  if (!cfd && !sb) return undefined;
  if (!cfd) return sb;
  if (!sb) return cfd;
  if (sharedClone) return { ...cfd, sample_scope: cfd.sample_scope || "shared_journal_once" };

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
