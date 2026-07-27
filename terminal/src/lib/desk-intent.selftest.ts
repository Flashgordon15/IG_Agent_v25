/**
 * Desk Intent confidence source + ranked rotator + multi-asset prefer.
 * Run: npx --yes tsx src/lib/desk-intent.selftest.ts
 * (from terminal/)
 */

import {
  applyDeskIntentHold,
  buildDeskIntentView,
  formatMarketHierarchy,
  initialDeskIntentHoldState,
  pickSniperForConfidence,
  resolvePreferFromMarketRows,
  resolveRotation,
  type DeskIntentHealthSlice,
  type DeskIntentOpsSlice,
  type DeskIntentRotationSlice,
  type DeskIntentSniperByEpic,
} from "./desk-intent";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(`ASSERT FAIL: ${msg}`);
}

const DOW = "IX.D.DOW.IFM.IP";
const GOLD = "CS.D.CFPGOLD.CFP.IP";
const EURUSD = "CS.D.EURUSD.CFD.IP";
const FTSE = "IX.D.FTSE.IFM.IP";
const NIKKEI = "IX.D.NIKKEI.IFM.IP";

const cfdPausedHealth: DeskIntentHealthSlice = {
  ok: false,
  trade_ready: true,
  trading_paused: true,
  agent_alive: true,
};

const sbArmedHealth: DeskIntentHealthSlice = {
  ok: false,
  trade_ready: true,
  trading_paused: false,
  agent_alive: true,
};

const cfdOps: DeskIntentOpsSlice = {
  trading_path_live: true,
  trading_path: { hot_path_epic: DOW },
  sniper_ml: {
    p_success: 0.79,
    approved: true,
    threshold: 0.68,
    epic: DOW,
  },
};

const sbOps: DeskIntentOpsSlice = {
  trading_path_live: true,
  trading_path: { hot_path_epic: DOW },
  sniper_ml: {
    p_success: 0.44,
    approved: false,
    threshold: 0.68,
    epic: DOW,
  },
};

const sbOpsMissing: DeskIntentOpsSlice = {
  trading_path_live: true,
  trading_path: { hot_path_epic: DOW },
  sniper_ml: null,
};

const sbRankedRot: DeskIntentRotationSlice = {
  ok: true,
  prefer_epic: GOLD,
  preference_reason: "prefer GOLD 71% over DOW 44%",
  per_epic_confidence: {
    [DOW]: { p_success: 0.44, approved: false, threshold: 0.68, mode: "WAIT" },
    [GOLD]: { p_success: 0.71, approved: true, threshold: 0.68, mode: "SETUP" },
  },
  rotation: {
    multi_source_auto_rotation: true,
    ranked_rotator: {
      active: true,
      mode: "ranked",
      dominant: GOLD,
      prefer_epic: GOLD,
      promoted: [GOLD, EURUSD],
      rows: [
        { epic: GOLD, eligible: true, rank: 1, p_success: 0.71, mode: "SETUP" },
        { epic: EURUSD, eligible: true, rank: 2, p_success: 0.55, mode: "WAIT" },
        { epic: DOW, eligible: true, rank: 3, p_success: 0.44, mode: "WAIT" },
        { epic: FTSE, eligible: false, rank: 4, p_success: 0.35, mode: "WAIT" },
      ],
    },
  },
};

const sbSniperByEpic: DeskIntentSniperByEpic = {
  ok: true,
  by_epic: {
    [DOW]: { p_success: 0.44, approved: false, threshold: 0.68 },
    [GOLD]: { p_success: 0.71, approved: true, threshold: 0.68 },
    [EURUSD]: { p_success: 0.55, approved: false, threshold: 0.7 },
    [FTSE]: { p_success: 0.35, approved: false, threshold: 0.68 },
    [NIKKEI]: { p_success: 0.99, approved: true, threshold: 0.68 },
  },
};

function main(): void {
  const pickedPaused = pickSniperForConfidence({
    cfdOps,
    sbOps,
    preferSb: true,
    focusEpic: DOW,
  });
  assert(pickedPaused.source === "SB macro", "preferSb must pick SB macro");
  assert(
    pickedPaused.sniper?.p_success === 0.44,
    "preferSb must use SB p_success not CFD 0.79",
  );

  // Critical: preferSb must NOT fall back to CFD even if SB has no score.
  const pickedNoSb = pickSniperForConfidence({
    cfdOps,
    sbOps: sbOpsMissing,
    preferSb: true,
    focusEpic: DOW,
  });
  assert(
    pickedNoSb.source === null && pickedNoSb.sniper == null,
    "preferSb must NOT fall back to CFD when SB sniper missing",
  );

  const pickedCfd = pickSniperForConfidence({
    cfdOps,
    sbOps,
    preferSb: false,
    focusEpic: DOW,
  });
  assert(pickedCfd.source === "CFD sniper", "preferSb=false picks CFD");
  assert(pickedCfd.sniper?.p_success === 0.79, "CFD p_success 0.79");

  const view = buildDeskIntentView({
    cfdOnline: true,
    sbOnline: true,
    cfdHealth: cfdPausedHealth,
    sbHealth: sbArmedHealth,
    cfdOps,
    sbOps,
    cfdRot: null,
    sbRot: sbRankedRot,
    sbSniperByEpic,
  });
  assert(view.engines[0].state === "PAUSED", "CFD A2 paused");
  assert(view.engines[1].state === "ARMED", "SB armed");
  assert(
    view.confidenceSource === "SB macro",
    `view source SB macro when CFD paused got ${view.confidenceSource}`,
  );
  assert(
    !String(view.confidenceSource || "").toLowerCase().includes("cfd"),
    "primary must never say cfd while A2 paused",
  );

  assert(view.confidenceAccounts.length === 2, "both accounts present");
  const [cfdRow, sbRow] = view.confidenceAccounts;
  assert(cfdRow.id === "cfd" && cfdRow.suppressed === true, "CFD suppressed");
  assert(
    cfdRow.line === "CFD: paused · —",
    `CFD line paused · — got ${cfdRow.line}`,
  );
  assert(cfdRow.mode == null && cfdRow.band === "—", "CFD no SETUP while paused");
  assert(sbRow.id === "sb" && sbRow.suppressed === false, "SB not suppressed");
  assert(sbRow.source === "SB macro", "SB account source SB macro");

  // Prefer GOLD from API — focus + hierarchy.
  assert(view.preferEpic === GOLD, `prefer GOLD got ${view.preferEpic}`);
  assert(view.preferMarket === "GOLD", `preferMarket GOLD got ${view.preferMarket}`);
  assert(view.focusMarket === "GOLD", `focus prefer GOLD got ${view.focusMarket}`);
  assert(
    view.marketHierarchy != null &&
      view.marketHierarchy.includes("DOW 44% WAIT") &&
      view.marketHierarchy.includes("GOLD 71% SETUP") &&
      view.marketHierarchy.includes("→ prefer GOLD"),
    `hierarchy prefer GOLD got ${view.marketHierarchy}`,
  );
  assert(
    !view.marketHierarchy!.includes("NIKKEI"),
    "Nikkei must not appear in hierarchy",
  );

  // Primary confidence tracks prefer GOLD (not DOW ops_strip).
  assert(
    view.confidencePct === 0.71,
    `primary pct prefer GOLD 0.71 got ${view.confidencePct}`,
  );
  assert(view.setupMode === "SETUP", "GOLD approved → SETUP");
  assert(view.confidenceBand === "High", "0.71 approved → High");

  // Even with only CFD score present, A2 pause must not adopt CFD as primary.
  const viewNoSbScore = buildDeskIntentView({
    cfdOnline: true,
    sbOnline: true,
    cfdHealth: cfdPausedHealth,
    sbHealth: sbArmedHealth,
    cfdOps,
    sbOps: sbOpsMissing,
    cfdRot: null,
    sbRot: null,
    sbSniperByEpic: null,
  });
  assert(
    viewNoSbScore.confidenceSource !== "CFD sniper",
    "A2+missing SB must not label CFD sniper",
  );
  assert(
    viewNoSbScore.confidenceAccounts[0].line === "CFD: paused · —",
    "CFD still suppressed when SB score missing",
  );
  assert(
    viewNoSbScore.confidencePct !== 0.79,
    "must not show CFD 79% as primary while A2 paused",
  );

  assert(view.rankedActive === true, "rankedActive from SB rotator");
  assert(
    view.promotedMarkets === "GOLD · EURUSD",
    `promoted GOLD · EURUSD got ${view.promotedMarkets}`,
  );

  // Client-side prefer from sniper_ml alone (no agent ranked_rotator yet).
  const clientOnly = buildDeskIntentView({
    cfdOnline: true,
    sbOnline: true,
    cfdHealth: cfdPausedHealth,
    sbHealth: sbArmedHealth,
    cfdOps,
    sbOps,
    cfdRot: null,
    sbRot: null,
    sbSniperByEpic,
  });
  assert(
    clientOnly.preferEpic === GOLD,
    `client sniper prefer GOLD got ${clientOnly.preferEpic}`,
  );
  assert(
    clientOnly.marketHierarchy?.includes("→ prefer GOLD"),
    `client hierarchy got ${clientOnly.marketHierarchy}`,
  );
  const prefer = resolvePreferFromMarketRows([
    { epic: DOW, label: "DOW", pct: 0.44, mode: "WAIT", approved: false },
    { epic: GOLD, label: "GOLD", pct: 0.71, mode: "SETUP", approved: true },
  ]);
  assert(prefer === GOLD, "resolvePreferFromMarketRows SETUP GOLD");
  const line = formatMarketHierarchy({
    rows: [
      { epic: DOW, label: "DOW", pct: 0.44, mode: "WAIT", approved: false },
      { epic: GOLD, label: "GOLD", pct: 0.71, mode: "SETUP", approved: true },
    ],
    preferEpic: GOLD,
  });
  assert(
    line === "DOW 44% WAIT · GOLD 71% SETUP → prefer GOLD",
    `exact hierarchy string got ${line}`,
  );

  const rot = resolveRotation({
    cfd: null,
    sb: sbRankedRot,
    focusEpic: GOLD,
    preferSb: true,
  });
  assert(rot.kind === "on", "ranked rotation kind on");
  assert(rot.label.startsWith("ranked"), "ranked label prefix");

  // Primary SB truth: ranked/per-epic can flash DOW SETUP while SB ops is WAIT —
  // mapper must not advertise primary SETUP against SB aggregate WAIT.
  const contradictionRot: DeskIntentRotationSlice = {
    ok: true,
    prefer_epic: DOW,
    preference_reason: "dominant DOW",
    per_epic_confidence: {
      [DOW]: { p_success: 0.7, approved: true, threshold: 0.68, mode: "SETUP" },
      [GOLD]: { p_success: 0.4, approved: false, threshold: 0.68, mode: "WAIT" },
    },
    rotation: {
      multi_source_auto_rotation: true,
      ranked_rotator: {
        active: true,
        mode: "ranked",
        dominant: DOW,
        prefer_epic: DOW,
        promoted: [DOW, GOLD],
      },
    },
  };
  const contradiction = buildDeskIntentView({
    cfdOnline: true,
    sbOnline: true,
    cfdHealth: cfdPausedHealth,
    sbHealth: sbArmedHealth,
    cfdOps,
    sbOps, // DOW 44% WAIT
    cfdRot: null,
    sbRot: contradictionRot,
    sbSniperByEpic: {
      ok: true,
      by_epic: {
        // Missing DOW → SB account falls back to ops_strip WAIT
        [GOLD]: { p_success: 0.4, approved: false, threshold: 0.68 },
      },
    },
  });
  assert(
    contradiction.confidenceAccounts[1].mode === "WAIT",
    `SB aggregate WAIT got ${contradiction.confidenceAccounts[1].mode}`,
  );
  assert(
    contradiction.setupMode === "WAIT",
    `primary must not SETUP while SB WAIT got ${contradiction.setupMode}`,
  );

  // SETUP hold: raw SETUP must not surface until holdMs elapses.
  let hold = initialDeskIntentHoldState();
  const t0 = 1_000_000;
  let step = applyDeskIntentHold({
    nowMs: t0,
    rawSetupMode: "SETUP",
    rawHierarchy: "DOW 70% SETUP → prefer DOW",
    prev: hold,
    setupHoldMs: 20_000,
    hierarchyDebounceMs: 12_000,
  });
  assert(step.setupMode === "WAIT", "SETUP not shown before hold");
  hold = step.state;
  step = applyDeskIntentHold({
    nowMs: t0 + 5_000,
    rawSetupMode: "SETUP",
    rawHierarchy: "DOW 70% SETUP → prefer DOW",
    prev: hold,
    setupHoldMs: 20_000,
    hierarchyDebounceMs: 12_000,
  });
  assert(step.setupMode === "WAIT", "SETUP still held at 5s");
  hold = step.state;
  step = applyDeskIntentHold({
    nowMs: t0 + 20_000,
    rawSetupMode: "SETUP",
    rawHierarchy: "DOW 70% SETUP → prefer DOW",
    prev: hold,
    setupHoldMs: 20_000,
    hierarchyDebounceMs: 12_000,
  });
  assert(step.setupMode === "SETUP", "SETUP after 20s hold");
  hold = step.state;
  step = applyDeskIntentHold({
    nowMs: t0 + 21_000,
    rawSetupMode: "WAIT",
    rawHierarchy: "DOW 44% WAIT → prefer DOW",
    prev: hold,
    setupHoldMs: 20_000,
    hierarchyDebounceMs: 12_000,
  });
  assert(step.setupMode === "WAIT", "WAIT demotes immediately");
  // Hierarchy: first non-null sticks; flicker candidate needs debounce.
  hold = step.state;
  const h1 = "DOW 44% WAIT · GOLD 71% SETUP → prefer GOLD";
  const h2 = "DOW 70% SETUP · GOLD 40% WAIT → prefer DOW";
  step = applyDeskIntentHold({
    nowMs: t0 + 22_000,
    rawSetupMode: "WAIT",
    rawHierarchy: h1,
    prev: initialDeskIntentHoldState(),
    hierarchyDebounceMs: 12_000,
  });
  assert(step.marketHierarchy === h1, "first hierarchy accepted");
  hold = step.state;
  step = applyDeskIntentHold({
    nowMs: t0 + 23_000,
    rawSetupMode: "WAIT",
    rawHierarchy: h2,
    prev: hold,
    hierarchyDebounceMs: 12_000,
  });
  assert(step.marketHierarchy === h1, "hierarchy flicker held");
  hold = step.state;
  step = applyDeskIntentHold({
    nowMs: t0 + 23_000 + 12_000,
    rawSetupMode: "WAIT",
    rawHierarchy: h2,
    prev: hold,
    hierarchyDebounceMs: 12_000,
  });
  assert(step.marketHierarchy === h2, "hierarchy updates after debounce");

  console.log("desk-intent.selftest: OK");
  console.log("  primary:", view.confidenceBand, view.confidencePct, view.confidenceSource);
  console.log("  hierarchy:", view.marketHierarchy);
  console.log("  accounts:", view.confidenceAccounts.map((r) => r.line).join(" | "));
  console.log("  rotation:", view.rotationLabel);
}

main();
