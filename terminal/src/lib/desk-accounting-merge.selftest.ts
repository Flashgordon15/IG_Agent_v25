/**
 * Focused assert for dual-port shared-journal PnL dedupe.
 * Run: npx --yes tsx src/lib/desk-accounting-merge.selftest.ts
 * (from terminal/)
 */

import {
  enrichClosedTradesFromJournal,
  isSharedGlobalJournalClone,
  mergeSimplifiedAccounting,
  type SimplifiedAccountingPayload,
} from "./desk-accounting-merge";
import {
  isSharedWeeklyClone,
  mergeWeeklyMetrics,
  type WeeklyMetricsPayload,
} from "./desk-weekly-metrics-merge";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(`ASSERT FAIL: ${msg}`);
}

function sharedDayPayload(today = -258.91): SimplifiedAccountingPayload {
  return {
    ok: true,
    source: "journal_csv",
    empty_day: false,
    today_net_realized_pnl_gbp: today,
    last_10_closed_trades: [
      {
        timestamp: "2026-07-24T10:14:42Z",
        asset: "DIAAAAX6GXWEEAM",
        direction: "SELL",
        net_pnl_gbp: -4.47,
        result: "LOSS",
      },
    ],
    daily_history: [
      { date: "2026-07-24", pnl_gbp: today },
      { date: "2026-07-23", pnl_gbp: 100 },
    ],
    performance_metrics: {
      wins: 12,
      losses: 66,
      sample_n: 78,
      net_true_outcome_gbp: today,
      gross_wins_gbp: 35.96,
      gross_losses_gbp: 294.87,
    },
  };
}

function distinctPayload(
  today: number,
  accountTag: "cfd" | "sb",
): SimplifiedAccountingPayload {
  return {
    ok: true,
    source: "account_filtered",
    empty_day: false,
    today_net_realized_pnl_gbp: today,
    last_10_closed_trades: [
      {
        timestamp: "2026-07-24T11:00:00Z",
        asset: accountTag === "cfd" ? "DOW" : "NIKKEI",
        direction: "SELL",
        net_pnl_gbp: today,
        account_id: accountTag === "cfd" ? "Z6BAH4" : "Z6BAH3",
      },
    ],
    daily_history: [{ date: "2026-07-24", pnl_gbp: today }],
    performance_metrics: {
      wins: 1,
      losses: 1,
      sample_n: 2,
      net_true_outcome_gbp: today,
      gross_wins_gbp: Math.max(today, 0),
      gross_losses_gbp: Math.max(-today, 0),
    },
  };
}

function sharedWeekly(): WeeklyMetricsPayload {
  return {
    ok: true,
    source: "journal_csv",
    merged: {
      weekly_sharpe: 1,
      asymmetric_profit_factor: 1.2,
      win_rate: 0.4,
      wins: 10,
      losses: 15,
      gross_wins_gbp: 500,
      gross_losses_gbp: 400,
      net_pnl_gbp: 100,
      sample_n: 25,
    },
    accounts: {
      Z6BAH4: {
        account_id: "Z6BAH4",
        product_type: "CFD",
        engine_origin: "QUANT_SNIPER",
        weekly_sharpe: 1,
        asymmetric_profit_factor: 1.1,
        win_rate: 0.35,
        wins: 4,
        losses: 8,
        gross_wins_gbp: 200,
        gross_losses_gbp: 280,
        net_pnl_gbp: -80,
        sample_n: 12,
      },
      Z6BAH3: {
        account_id: "Z6BAH3",
        product_type: "SPREADBET",
        engine_origin: "MACRO_SENTINEL",
        weekly_sharpe: 1,
        asymmetric_profit_factor: 1.3,
        win_rate: 0.45,
        wins: 6,
        losses: 7,
        gross_wins_gbp: 300,
        gross_losses_gbp: 120,
        net_pnl_gbp: 180,
        sample_n: 13,
      },
    },
    asset_breakdown: [],
  };
}

function main(): void {
  const cfd = sharedDayPayload(-258.91);
  const sb = sharedDayPayload(-258.91);
  assert(isSharedGlobalJournalClone(cfd, sb), "forensic payloads should be shared clones");

  const merged = mergeSimplifiedAccounting(cfd, sb);
  assert(merged != null, "merged payload present");
  assert(
    Math.abs((merged!.today_net_realized_pnl_gbp ?? 0) - -258.91) < 0.001,
    `combined today must be −258.91 once, got ${merged!.today_net_realized_pnl_gbp}`,
  );
  assert(
    merged!.system_state?.shared_journal_deduped === true,
    "shared_journal_deduped flag set",
  );
  assert(
    (merged!.last_10_closed_trades ?? []).length === 1,
    "closed trades must not be duplicated across ports",
  );
  assert(
    (merged!.daily_history ?? []).find((d) => d.date === "2026-07-24")?.pnl_gbp === -258.91,
    "daily history day must not be doubled",
  );
  // SHARED journal dedupe must NOT stamp placeholder JOURNAL/SHARED on rows
  // that lack identity — leave blank for enrichClosedTradesFromJournal.
  const bare = merged!.last_10_closed_trades![0]!;
  assert(
    bare.account_id !== "SHARED" && bare.product_type !== "JOURNAL",
    "shared merge must not invent SHARED/JOURNAL row labels",
  );

  const enriched = enrichClosedTradesFromJournal(
    [
      {
        timestamp: "2026-07-24T12:08:00Z",
        asset: "DIAAAAX6HA3F6AR",
        direction: "SELL",
        net_pnl_gbp: -9.08,
      },
    ],
    [
      {
        dealId: "DIAAAAX6HA3F6AR",
        accountId: "Z6BAH4",
        productType: "CFD",
        engineOrigin: "broker_attached",
      },
    ],
    [{ dealId: "6HA3F6AR", epic: "IX.D.NIKKEI.IFM.IP", market: "Japan 225" }],
  );
  assert(enriched[0]!.account_id === "Z6BAH4", "enrich account from journal");
  assert(enriched[0]!.product_type === "CFD", "enrich product from journal");
  assert(enriched[0]!.asset === "NIKKEI", "enrich human asset from epic");

  const distinct = mergeSimplifiedAccounting(
    distinctPayload(-79.69, "cfd"),
    distinctPayload(-179.22, "sb"),
  );
  assert(distinct != null, "distinct merge present");
  assert(
    Math.abs((distinct!.today_net_realized_pnl_gbp ?? 0) - -258.91) < 0.01,
    `distinct accounts should sum to −258.91, got ${distinct!.today_net_realized_pnl_gbp}`,
  );
  assert(
    distinct!.system_state?.shared_journal_deduped !== true,
    "distinct contributions must not be marked shared-deduped",
  );

  const weekA = sharedWeekly();
  const weekB = sharedWeekly();
  assert(isSharedWeeklyClone(weekA, weekB), "weekly shared clone detected");
  const weekMerged = mergeWeeklyMetrics(weekA, weekB);
  assert(weekMerged != null, "weekly merged present");
  assert(
    Math.abs((weekMerged!.merged.net_pnl_gbp ?? 0) - 100) < 0.01,
    `weekly net must stay 100 once, got ${weekMerged!.merged.net_pnl_gbp}`,
  );
  assert(weekMerged!.shared_journal_deduped === true, "weekly shared_journal_deduped");

  console.log("desk-accounting-merge.selftest: OK");
}

main();
