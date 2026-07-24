/**
 * Desk Intent — pure mapping for the pilot strip.
 *
 * Answers three questions only:
 * 1. Can this engine trade? (ARMED | PAUSED | BLOCKED)
 * 2. Confidence on the focus market?
 * 3. Is rotation occurring?
 *
 * A2 semantics: CFD :8080 `health.trading_paused` → PAUSED · "A2 · entries paused"
 * (health is SoT for entry freeze; do not prefer ops_strip halt_flags over it).
 */

export type DeskIntentState = "ARMED" | "PAUSED" | "BLOCKED";

export type DeskIntentEngineId = "cfd" | "sb";

export type DeskIntentEngineView = {
  id: DeskIntentEngineId;
  shortLabel: string;
  origin: string;
  state: DeskIntentState;
  why: string | null;
};

export type DeskIntentConfidenceBand = "Low" | "Med" | "High" | "—";
export type DeskIntentSetupMode = "WAIT" | "SETUP" | null;

export type DeskIntentRotationKind =
  | "off"
  | "scanning"
  | "on"
  | "held";

/** Explicit per-account source — never imply CFD while A2-paused. */
export type DeskIntentConfidenceSource =
  | "SB macro"
  | "SB sniper"
  | "CFD sniper"
  | null;

export type DeskIntentAccountConfidence = {
  id: DeskIntentEngineId;
  /** e.g. "CFD: paused · —" or "SB: High 77% · SETUP" */
  line: string;
  band: DeskIntentConfidenceBand;
  pct: number | null;
  mode: DeskIntentSetupMode;
  source: DeskIntentConfidenceSource;
  /** True when engine paused/blocked — scores must not imply SETUP. */
  suppressed: boolean;
};

export type DeskIntentRankedRow = {
  epic?: string;
  eligible?: boolean;
  rank?: number;
  confidence?: number | null;
  p_success?: number | null;
  approved?: boolean | null;
  threshold?: number | null;
  mode?: string | null;
  score?: number | null;
};

export type DeskIntentRankedRotator = {
  active?: boolean;
  mode?: string;
  dominant?: string | null;
  promoted?: string[];
  reason?: string;
  rows?: DeskIntentRankedRow[];
  prefer_epic?: string | null;
  preference_reason?: string | null;
  per_epic_confidence?: Record<string, DeskIntentEpicConfidence> | null;
};

export type DeskIntentEpicConfidence = {
  p_success?: number | null;
  confidence?: number | null;
  approved?: boolean | null;
  threshold?: number | null;
  mode?: string | null;
  score?: number | null;
  rank?: number | null;
  eligible?: boolean | null;
};

export type DeskIntentSniperByEpic = {
  ok?: boolean;
  by_epic?: Record<
    string,
    {
      p_success?: number | null;
      approved?: boolean | null;
      threshold?: number | null;
    }
  > | null;
};

export type DeskIntentView = {
  engines: DeskIntentEngineView[];
  focusMarket: string;
  focusEpic: string | null;
  /** Short labels for ranked promoted set, e.g. "DOW · GOLD" */
  promotedMarkets: string;
  rankedActive: boolean;
  /** Primary confidence = armed / prefer account (SB when CFD A2-paused). */
  confidenceBand: DeskIntentConfidenceBand;
  confidencePct: number | null;
  confidenceSource: DeskIntentConfidenceSource;
  setupMode: DeskIntentSetupMode;
  /** Always both accounts — operator must see CFD vs SB without ambiguity. */
  confidenceAccounts: DeskIntentAccountConfidence[];
  /**
   * Multi-asset hierarchy, e.g.
   * "DOW 44% WAIT · GOLD 71% SETUP → prefer GOLD"
   */
  marketHierarchy: string | null;
  preferEpic: string | null;
  preferMarket: string | null;
  preferenceReason: string | null;
  rotationKind: DeskIntentRotationKind;
  rotationLabel: string;
  nextBlock: string | null;
  sbPostureHint: string | null;
};

export type DeskIntentHealthSlice = {
  ok?: boolean;
  trading_healthy?: boolean;
  trade_ready?: boolean;
  trading_paused?: boolean;
  agent_alive?: boolean;
  port_bound?: boolean;
  status?: string;
  ready?: boolean;
};

export type DeskIntentOpsSlice = {
  core_detached?: boolean;
  halt_active?: boolean;
  entries_paused?: boolean;
  trading_path_live?: boolean;
  trading_path_primary?: { code?: string; label?: string; id?: string } | null;
  trading_path_blockers?: Array<string | { code?: string; label?: string }> | null;
  trading_path?: {
    hot_path_epic?: string;
    primary_blocker?: { code?: string; label?: string; id?: string } | null;
    blockers?: Array<string | { code?: string; label?: string }> | null;
  } | null;
  halt_flags?: {
    entry_halt?: boolean;
    trading_paused?: boolean;
    offline_for_dev?: boolean;
    deploy_hold?: boolean;
  } | null;
  sniper_ml?: {
    p_success?: number | null;
    approved?: boolean | null;
    threshold?: number | null;
    epic?: string | null;
  } | null;
  desk_idle_reason?: { code?: string; label?: string } | null;
  desk_stability?: { grade?: string; label?: string } | null;
  desk_liveness?: { ok?: boolean | null } | null;
};

export type DeskIntentRotationSlice = {
  ok?: boolean;
  prefer_epic?: string | null;
  preference_reason?: string | null;
  per_epic_confidence?: Record<string, DeskIntentEpicConfidence> | null;
  ranked_rotator?: DeskIntentRankedRotator | null;
  rotation?: {
    multi_source_auto_rotation?: boolean;
    pinned_open_epics?: string[];
    last_rotation_at?: number;
    last_rotation_reason?: string;
    active_instruments?: Array<{ epic?: string; label?: string }>;
    ranked_rotator?: DeskIntentRankedRotator | null;
    prefer_epic?: string | null;
    preference_reason?: string | null;
    per_epic_confidence?: Record<string, DeskIntentEpicConfidence> | null;
  } | null;
};

/** Ranked Intent candidates — Nikkei excluded until JPY PnL certified. */
export const RANKED_INTENT_CANDIDATES = [
  "IX.D.DOW.IFM.IP",
  "CS.D.CFPGOLD.CFP.IP",
  "CS.D.EURUSD.CFD.IP",
  "IX.D.FTSE.IFM.IP",
] as const;

/** Prefer SB ranked snapshot when CFD is paused/blocked (A2). */
export function pickRankedRotator(args: {
  cfd: DeskIntentRotationSlice | null;
  sb: DeskIntentRotationSlice | null;
  preferSb: boolean;
}): DeskIntentRankedRotator | null {
  const order = args.preferSb
    ? [args.sb, args.cfd]
    : [args.cfd, args.sb];
  for (const slice of order) {
    const rr =
      slice?.ranked_rotator ||
      slice?.rotation?.ranked_rotator ||
      null;
    if (rr?.active === true) return rr;
  }
  // Inactive but present — still useful for prefer_epic / rows after partial deploy.
  for (const slice of order) {
    const rr =
      slice?.ranked_rotator ||
      slice?.rotation?.ranked_rotator ||
      null;
    if (rr) return rr;
  }
  return null;
}

export function pickPreferEpicFromRotation(args: {
  cfd: DeskIntentRotationSlice | null;
  sb: DeskIntentRotationSlice | null;
  preferSb: boolean;
}): { preferEpic: string | null; preferenceReason: string | null } {
  const order = args.preferSb
    ? [args.sb, args.cfd]
    : [args.cfd, args.sb];
  for (const slice of order) {
    const fromTop = slice?.prefer_epic ? String(slice.prefer_epic).trim() : "";
    const fromRot = slice?.rotation?.prefer_epic
      ? String(slice.rotation.prefer_epic).trim()
      : "";
    const rr =
      slice?.ranked_rotator || slice?.rotation?.ranked_rotator || null;
    const fromRr = rr?.prefer_epic ? String(rr.prefer_epic).trim() : "";
    const fromDom =
      rr?.active && rr.dominant ? String(rr.dominant).trim() : "";
    const epic = fromTop || fromRot || fromRr || fromDom || "";
    if (epic) {
      const reason =
        slice?.preference_reason ||
        slice?.rotation?.preference_reason ||
        rr?.preference_reason ||
        null;
      return {
        preferEpic: epic,
        preferenceReason: reason ? String(reason) : null,
      };
    }
  }
  return { preferEpic: null, preferenceReason: null };
}

export function formatMarketList(
  epics: Array<string | null | undefined>,
  sep = ",",
): string {
  const labels = epics
    .map((e) => shortMarketLabel(e))
    .filter((l) => l && l !== "—");
  const seen = new Set<string>();
  const uniq: string[] = [];
  for (const l of labels) {
    if (seen.has(l)) continue;
    seen.add(l);
    uniq.push(l);
  }
  return uniq.join(sep);
}

/** Candidates not in promoted — eligible waiters first, then remaining rows. */
export function rankedWaitingEpics(rr: DeskIntentRankedRotator | null): string[] {
  if (!rr) return [];
  const promoted = new Set(
    (rr.promoted || []).map((e) => String(e).trim()).filter(Boolean),
  );
  const rows = [...(rr.rows || [])].sort(
    (a, b) => Number(a.rank ?? 99) - Number(b.rank ?? 99),
  );
  const out: string[] = [];
  for (const row of rows) {
    const epic = String(row.epic || "").trim();
    if (!epic || promoted.has(epic)) continue;
    out.push(epic);
  }
  return out;
}

const EPIC_SHORT: Record<string, string> = {
  "IX.D.DOW.IFM.IP": "DOW",
  "IX.D.NIKKEI.IFM.IP": "NIKKEI",
  "CS.D.CFPGOLD.CFP.IP": "GOLD",
  "CS.D.EURUSD.CFD.IP": "EURUSD",
  "CS.D.CRUDE.CFD.IP": "CRUDE",
  "IX.D.FTSE.IFM.IP": "FTSE",
  "IX.D.DAX.IFM.IP": "DAX",
};

/** Dual-regime SB posture — MACRO_SENTINEL is macro / long_trade_runner only. */
export const SB_DUAL_REGIME_HINT = "macro/LTR";

export function shortMarketLabel(epic: string | null | undefined): string {
  const e = String(epic || "").trim();
  if (!e) return "—";
  if (EPIC_SHORT[e]) return EPIC_SHORT[e];
  const parts = e.split(".");
  for (const p of parts) {
    const u = p.toUpperCase();
    if (["DOW", "NIKKEI", "GOLD", "FTSE", "DAX", "CRUDE", "EURUSD"].includes(u)) {
      return u === "EURUSD" ? "EURUSD" : u;
    }
  }
  return e.length > 12 ? e.slice(0, 12) : e;
}

function healthReachable(h: DeskIntentHealthSlice | null | undefined): boolean {
  if (!h || typeof h !== "object") return false;
  if (h.agent_alive === false) return false;
  if (h.trading_healthy === true) return true;
  if (h.port_bound === true) return true;
  if (h.trade_ready === true) return true;
  if (h.ready === true) return true;
  if (h.ok === true) return true;
  const status = String(h.status ?? "").toUpperCase();
  if (status === "OPERATIONAL") return true;
  // Live desks often return ok:false with trade_ready/agent_alive still true.
  return Boolean(h.agent_alive) || Boolean(h.port_bound);
}

function primaryBlockerLabel(
  ops: DeskIntentOpsSlice | null | undefined,
): string | null {
  if (!ops) return null;
  const raw =
    ops.trading_path_primary ||
    ops.trading_path?.primary_blocker ||
    ops.desk_idle_reason ||
    null;
  if (raw) {
    const ext = raw as { code?: string; label?: string; id?: string };
    const label = String(ext.label || ext.code || ext.id || "").trim();
    if (label) return clipWhy(label);
  }
  const blockers = ops.trading_path_blockers || ops.trading_path?.blockers || [];
  if (Array.isArray(blockers) && blockers.length > 0) {
    const first = blockers[0];
    if (typeof first === "string" && first.trim()) return clipWhy(first.trim());
    if (first && typeof first === "object") {
      const label = String(first.label || first.code || "").trim();
      if (label) return clipWhy(label);
    }
  }
  return null;
}

function clipWhy(s: string, max = 42): string {
  const t = s.replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  return `${t.slice(0, max - 1)}…`;
}

export function resolveEngineIntent(args: {
  id: DeskIntentEngineId;
  online: boolean;
  health: DeskIntentHealthSlice | null;
  ops: DeskIntentOpsSlice | null;
}): DeskIntentEngineView {
  const { id, online, health, ops } = args;
  const shortLabel = id === "cfd" ? "CFD" : "SB";
  const origin = id === "cfd" ? "QUANT_SNIPER" : "MACRO_SENTINEL";

  if (!online || !healthReachable(health)) {
    return {
      id,
      shortLabel,
      origin,
      state: "BLOCKED",
      why: id === "cfd" ? "port offline · :8080" : "port offline · :8081",
    };
  }

  if (ops?.core_detached) {
    return {
      id,
      shortLabel,
      origin,
      state: "BLOCKED",
      why: "harness · core detached",
    };
  }

  const halt = ops?.halt_flags;
  if (ops?.halt_active || halt?.offline_for_dev) {
    return {
      id,
      shortLabel,
      origin,
      state: "BLOCKED",
      why: "api stop · offline_for_dev",
    };
  }
  if (halt?.deploy_hold) {
    return {
      id,
      shortLabel,
      origin,
      state: "BLOCKED",
      why: "api stop · deploy hold",
    };
  }

  const stab = String(ops?.desk_stability?.grade || "").toUpperCase();
  if (stab === "R" && ops?.desk_liveness?.ok === false) {
    return {
      id,
      shortLabel,
      origin,
      state: "BLOCKED",
      why: "harness · desk critical",
    };
  }

  // A2 / operator freeze — health is SoT (CFD often paused while ops halt_flags clear).
  const paused =
    health?.trading_paused === true ||
    ops?.entries_paused === true ||
    halt?.entry_halt === true ||
    halt?.trading_paused === true;

  if (paused) {
    const why =
      id === "cfd" && health?.trading_paused === true
        ? "A2 · entries paused"
        : halt?.entry_halt
          ? "entry halt"
          : "entries paused";
    return { id, shortLabel, origin, state: "PAUSED", why };
  }

  const pathLive =
    ops?.trading_path_live === true ||
    health?.trade_ready === true ||
    health?.trading_healthy === true;

  if (!pathLive && ops?.trading_path_live === false) {
    return {
      id,
      shortLabel,
      origin,
      state: "BLOCKED",
      why: primaryBlockerLabel(ops) || "path down",
    };
  }

  return {
    id,
    shortLabel,
    origin,
    state: "ARMED",
    why: id === "sb" ? SB_DUAL_REGIME_HINT : null,
  };
}

/** SB MACRO_SENTINEL → "SB macro"; CFD QUANT_SNIPER → "CFD sniper". */
export function confidenceSourceForEngine(
  id: DeskIntentEngineId,
): Exclude<DeskIntentConfidenceSource, null> {
  return id === "sb" ? "SB macro" : "CFD sniper";
}

function hasSniperP(
  s: DeskIntentOpsSlice["sniper_ml"] | null | undefined,
): boolean {
  return s?.p_success != null && Number.isFinite(Number(s.p_success));
}

/**
 * Pick primary confidence score.
 * When preferSb (CFD A2 paused/blocked): NEVER fall back to CFD — that was
 * the trust-breaking "High · SETUP cfd sniper" while CFD entries were frozen.
 */
export function pickSniperForConfidence(args: {
  cfdOps: DeskIntentOpsSlice | null;
  sbOps: DeskIntentOpsSlice | null;
  preferSb: boolean;
  focusEpic: string | null;
}): {
  sniper: DeskIntentOpsSlice["sniper_ml"] | null;
  source: DeskIntentConfidenceSource;
} {
  const focus = args.focusEpic ? String(args.focusEpic).trim() : "";
  const ordered: Array<{
    source: Exclude<DeskIntentConfidenceSource, null>;
    sniper: DeskIntentOpsSlice["sniper_ml"] | null | undefined;
  }> = args.preferSb
    ? [{ source: "SB macro", sniper: args.sbOps?.sniper_ml }]
    : [
        { source: "CFD sniper", sniper: args.cfdOps?.sniper_ml },
        { source: "SB macro", sniper: args.sbOps?.sniper_ml },
      ];

  if (focus) {
    for (const row of ordered) {
      if (
        hasSniperP(row.sniper) &&
        String(row.sniper?.epic || "").trim() === focus
      ) {
        return { sniper: row.sniper ?? null, source: row.source };
      }
    }
  }
  for (const row of ordered) {
    if (hasSniperP(row.sniper)) {
      return { sniper: row.sniper ?? null, source: row.source };
    }
  }
  return { sniper: null, source: null };
}

function fmtConfidencePct(p: number | null): string {
  if (p == null || !Number.isFinite(p)) return "";
  return `${(p * 100).toFixed(0)}%`;
}

/** Build one account's confidence row (suppressed when paused/blocked). */
export function resolveAccountConfidence(args: {
  id: DeskIntentEngineId;
  engine: DeskIntentEngineView;
  sniper: DeskIntentOpsSlice["sniper_ml"] | null | undefined;
  focusEpic: string | null;
}): DeskIntentAccountConfidence {
  const source = confidenceSourceForEngine(args.id);
  const suppressed =
    args.engine.state === "PAUSED" || args.engine.state === "BLOCKED";
  const stateWord =
    args.engine.state === "PAUSED"
      ? "paused"
      : args.engine.state === "BLOCKED"
        ? "blocked"
        : null;

  if (suppressed) {
    return {
      id: args.id,
      line: `${args.engine.shortLabel}: ${stateWord} · —`,
      band: "—",
      pct: null,
      mode: null,
      source,
      suppressed: true,
    };
  }

  const conf = resolveConfidence({
    sniper: args.sniper,
    focusEpic: args.focusEpic,
  });
  const parts = [`${args.engine.shortLabel}:`];
  if (conf.pct == null) {
    parts.push("—");
  } else {
    parts.push(conf.band);
    const pct = fmtConfidencePct(conf.pct);
    if (pct) parts.push(pct);
    if (conf.mode) parts.push(`· ${conf.mode}`);
  }
  return {
    id: args.id,
    line: parts.join(" ").replace(/\s+/g, " ").trim(),
    band: conf.band,
    pct: conf.pct,
    mode: conf.mode,
    source,
    suppressed: false,
  };
}

export function resolveConfidence(args: {
  sniper: DeskIntentOpsSlice["sniper_ml"] | null | undefined;
  focusEpic: string | null;
}): {
  band: DeskIntentConfidenceBand;
  pct: number | null;
  mode: DeskIntentSetupMode;
} {
  const sniper = args.sniper;
  const p =
    sniper?.p_success != null && Number.isFinite(Number(sniper.p_success))
      ? Number(sniper.p_success)
      : null;
  if (p == null) {
    return { band: "—", pct: null, mode: null };
  }
  const thr =
    sniper?.threshold != null && Number.isFinite(Number(sniper.threshold))
      ? Number(sniper.threshold)
      : 0.68;
  const approved = sniper?.approved === true;
  let band: DeskIntentConfidenceBand;
  if (approved && p >= thr) band = "High";
  else if (p >= thr * 0.85) band = "Med";
  else band = "Low";
  const mode: DeskIntentSetupMode = approved && p >= thr ? "SETUP" : "WAIT";
  return { band, pct: p, mode };
}

export function resolveRotation(args: {
  cfd: DeskIntentRotationSlice | null;
  sb: DeskIntentRotationSlice | null;
  focusEpic: string | null;
  preferSb: boolean;
}): { kind: DeskIntentRotationKind; label: string } {
  const pick = (slice: DeskIntentRotationSlice | null) => slice?.rotation ?? null;
  const cfdR = pick(args.cfd);
  const sbR = pick(args.sb);

  const pinned = [
    ...(cfdR?.pinned_open_epics || []),
    ...(sbR?.pinned_open_epics || []),
  ].filter(Boolean);
  if (pinned.length > 0) {
    const m = shortMarketLabel(String(pinned[0]));
    return { kind: "held", label: `held on open · ${m}` };
  }

  const primary = args.preferSb ? sbR || cfdR : cfdR || sbR;
  const secondary = args.preferSb ? cfdR : sbR;
  const rot = primary || secondary;
  if (!rot) return { kind: "off", label: "off" };

  const ranked = pickRankedRotator({
    cfd: args.cfd,
    sb: args.sb,
    preferSb: args.preferSb,
  });
  if (ranked?.active) {
    const dominant = shortMarketLabel(ranked.dominant);
    const promoted = formatMarketList(ranked.promoted || [], ",");
    const waiting = formatMarketList(rankedWaitingEpics(ranked), "·");
    const parts = ["ranked"];
    if (dominant !== "—") parts.push(`dominant ${dominant}`);
    if (promoted) parts.push(`promoted ${promoted}`);
    if (waiting) parts.push(`wait ${waiting}`);
    return { kind: "on", label: parts.join(" · ") };
  }

  const multi =
    rot.multi_source_auto_rotation === true ||
    cfdR?.multi_source_auto_rotation === true ||
    sbR?.multi_source_auto_rotation === true;

  if (!multi) return { kind: "off", label: "off" };

  const active = rot.active_instruments || [];
  const focus =
    args.focusEpic ||
    (active[0]?.epic ? String(active[0].epic) : null);
  const m = shortMarketLabel(focus);
  if (active.length > 0 && focus) {
    return { kind: "on", label: `on ${m}` };
  }
  return { kind: "scanning", label: `scanning · ${m}` };
}

export function resolveFocusEpic(args: {
  cfdOps: DeskIntentOpsSlice | null;
  sbOps: DeskIntentOpsSlice | null;
  cfdRot: DeskIntentRotationSlice | null;
  sbRot: DeskIntentRotationSlice | null;
  bufferFocus?: string | null;
  preferSb: boolean;
  preferEpic?: string | null;
}): string | null {
  if (args.preferEpic && String(args.preferEpic).trim()) {
    return String(args.preferEpic).trim();
  }
  const fromOps = (ops: DeskIntentOpsSlice | null) =>
    ops?.trading_path?.hot_path_epic
      ? String(ops.trading_path.hot_path_epic)
      : null;
  const fromRanked = (rot: DeskIntentRotationSlice | null) => {
    const rr = rot?.ranked_rotator || rot?.rotation?.ranked_rotator;
    const d = rr?.prefer_epic || rr?.dominant;
    return d && rr?.active ? String(d) : null;
  };
  const fromRot = (rot: DeskIntentRotationSlice | null) => {
    const a = rot?.rotation?.active_instruments?.[0]?.epic;
    return a ? String(a) : null;
  };
  const order = args.preferSb
    ? [
        fromRanked(args.sbRot),
        fromOps(args.sbOps),
        fromOps(args.cfdOps),
        fromRot(args.sbRot),
        fromRot(args.cfdRot),
        args.bufferFocus,
      ]
    : [
        fromRanked(args.cfdRot),
        fromOps(args.cfdOps),
        fromOps(args.sbOps),
        fromRot(args.cfdRot),
        fromRot(args.sbRot),
        args.bufferFocus,
      ];
  for (const e of order) {
    if (e && String(e).trim()) return String(e).trim();
  }
  return null;
}

export type DeskIntentMarketConfRow = {
  epic: string;
  label: string;
  pct: number;
  mode: DeskIntentSetupMode;
  approved: boolean;
};

/**
 * Build per-candidate confidence rows from API prefer map and/or live sniper_ml.
 * Nikkei never appears. Used for hierarchy string + client-side prefer.
 */
export function collectRankedMarketConfidence(args: {
  perEpic?: Record<string, DeskIntentEpicConfidence> | null;
  sniperByEpic?: DeskIntentSniperByEpic | null;
  rankedRows?: DeskIntentRankedRow[] | null;
  candidates?: readonly string[];
}): DeskIntentMarketConfRow[] {
  const candidates = args.candidates || RANKED_INTENT_CANDIDATES;
  const out: DeskIntentMarketConfRow[] = [];
  for (const epic of candidates) {
    if (epic.includes("NIKKEI")) continue;
    let p: number | null = null;
    let approved = false;
    let thr = 0.68;
    const fromMap = args.perEpic?.[epic];
    const fromRow = (args.rankedRows || []).find(
      (r) => String(r.epic || "").trim() === epic,
    );
    const fromSniper = args.sniperByEpic?.by_epic?.[epic];
    const pick =
      fromMap?.p_success != null || fromMap?.confidence != null
        ? fromMap
        : fromRow?.p_success != null || fromRow?.confidence != null
          ? fromRow
          : fromSniper;
    if (!pick) continue;
    const rawP =
      (pick as DeskIntentEpicConfidence).p_success ??
      (pick as DeskIntentEpicConfidence).confidence ??
      null;
    if (rawP == null || !Number.isFinite(Number(rawP))) continue;
    p = Number(rawP);
    const rawThr = (pick as { threshold?: number | null }).threshold;
    if (rawThr != null && Number.isFinite(Number(rawThr))) thr = Number(rawThr);
    approved = (pick as { approved?: boolean | null }).approved === true;
    const mode: DeskIntentSetupMode =
      approved && p >= thr ? "SETUP" : "WAIT";
    out.push({
      epic,
      label: shortMarketLabel(epic),
      pct: p,
      mode,
      approved,
    });
  }
  return out;
}

/** Prefer highest SETUP confidence; else highest p_success. */
export function resolvePreferFromMarketRows(
  rows: DeskIntentMarketConfRow[],
): string | null {
  if (!rows.length) return null;
  const setups = rows.filter((r) => r.mode === "SETUP");
  const pool = setups.length ? setups : rows;
  const best = [...pool].sort((a, b) => b.pct - a.pct)[0];
  return best?.epic ?? null;
}

/**
 * "DOW 44% WAIT · GOLD 71% SETUP → prefer GOLD"
 * Shows ranked candidates that have scores (stable epic order) + prefer tail.
 */
export function formatMarketHierarchy(args: {
  rows: DeskIntentMarketConfRow[];
  preferEpic: string | null;
  maxMarkets?: number;
}): string | null {
  const { rows, preferEpic } = args;
  if (!rows.length) return null;
  const max = args.maxMarkets ?? 4;
  const order = new Map<string, number>(
    RANKED_INTENT_CANDIDATES.map((e, i) => [e as string, i]),
  );
  const selected = [...rows]
    .sort((a, b) => (order.get(a.epic) ?? 99) - (order.get(b.epic) ?? 99))
    .slice(0, max);
  const parts = selected.map((r) => {
    const pct = `${(r.pct * 100).toFixed(0)}%`;
    return `${r.label} ${pct} ${r.mode || "WAIT"}`;
  });
  const preferLabel = preferEpic ? shortMarketLabel(preferEpic) : "—";
  return `${parts.join(" · ")} → prefer ${preferLabel}`;
}

export function buildDeskIntentView(args: {
  cfdOnline: boolean;
  sbOnline: boolean;
  cfdHealth: DeskIntentHealthSlice | null;
  sbHealth: DeskIntentHealthSlice | null;
  cfdOps: DeskIntentOpsSlice | null;
  sbOps: DeskIntentOpsSlice | null;
  cfdRot: DeskIntentRotationSlice | null;
  sbRot: DeskIntentRotationSlice | null;
  bufferFocus?: string | null;
  /** Live /api/desk/sniper_ml — preferred SB while CFD A2-paused. */
  sbSniperByEpic?: DeskIntentSniperByEpic | null;
  cfdSniperByEpic?: DeskIntentSniperByEpic | null;
}): DeskIntentView {
  const cfdEngine = resolveEngineIntent({
    id: "cfd",
    online: args.cfdOnline,
    health: args.cfdHealth,
    ops: args.cfdOps,
  });
  const sbEngine = resolveEngineIntent({
    id: "sb",
    online: args.sbOnline,
    health: args.sbHealth,
    ops: args.sbOps,
  });

  // When CFD is A2-paused, prefer SB rotation / focus for the shared strip.
  const preferSb =
    cfdEngine.state === "PAUSED" || cfdEngine.state === "BLOCKED";

  const ranked = pickRankedRotator({
    cfd: args.cfdRot,
    sb: args.sbRot,
    preferSb,
  });

  const fromApi = pickPreferEpicFromRotation({
    cfd: args.cfdRot,
    sb: args.sbRot,
    preferSb,
  });

  const sniperForHierarchy = preferSb
    ? args.sbSniperByEpic
    : args.cfdSniperByEpic || args.sbSniperByEpic;

  const perEpic =
    (preferSb
      ? args.sbRot?.per_epic_confidence ||
        args.sbRot?.rotation?.per_epic_confidence ||
        ranked?.per_epic_confidence
      : args.cfdRot?.per_epic_confidence ||
        args.cfdRot?.rotation?.per_epic_confidence ||
        ranked?.per_epic_confidence) || null;

  const marketRows = collectRankedMarketConfidence({
    perEpic,
    sniperByEpic: sniperForHierarchy,
    rankedRows: ranked?.rows || null,
  });

  const clientPrefer = resolvePreferFromMarketRows(marketRows);
  const preferEpic = fromApi.preferEpic || clientPrefer;
  const preferenceReason =
    fromApi.preferenceReason ||
    (clientPrefer
      ? `client prefer ${shortMarketLabel(clientPrefer)} from sniper_ml`
      : null);

  const focusEpic = resolveFocusEpic({
    cfdOps: args.cfdOps,
    sbOps: args.sbOps,
    cfdRot: args.cfdRot,
    sbRot: args.sbRot,
    bufferFocus: args.bufferFocus,
    preferSb,
    preferEpic,
  });

  const confidenceAccounts: DeskIntentAccountConfidence[] = [
    resolveAccountConfidence({
      id: "cfd",
      engine: cfdEngine,
      sniper: args.cfdOps?.sniper_ml,
      focusEpic,
    }),
    resolveAccountConfidence({
      id: "sb",
      engine: sbEngine,
      sniper: resolveSniperForPreferEpic({
        ops: args.sbOps,
        sniperByEpic: args.sbSniperByEpic,
        preferEpic,
        focusEpic,
      }),
      focusEpic,
    }),
  ];

  // Primary = SB when CFD A2-paused/blocked; else CFD (fall back to SB only if CFD suppressed).
  // Never surface CFD SETUP/High as primary while CFD entries are frozen.
  const primaryAccount = preferSb
    ? confidenceAccounts[1]
    : confidenceAccounts[0].suppressed
      ? confidenceAccounts[1]
      : confidenceAccounts[0];

  const rotation = resolveRotation({
    cfd: args.cfdRot,
    sb: args.sbRot,
    focusEpic,
    preferSb,
  });

  const promotedMarkets = ranked?.active
    ? formatMarketList(ranked.promoted || [], " · ") || "—"
    : "—";

  const marketHierarchy = formatMarketHierarchy({
    rows: marketRows,
    preferEpic,
  });

  // One-line top veto only when an engine is BLOCKED — never dump lists / A2 noise.
  const rawNext =
    preferSb
      ? primaryBlockerLabel(args.sbOps) || primaryBlockerLabel(args.cfdOps)
      : primaryBlockerLabel(args.cfdOps) || primaryBlockerLabel(args.sbOps);
  const nextBlock =
    rawNext &&
    (cfdEngine.state === "BLOCKED" || sbEngine.state === "BLOCKED")
      ? rawNext
      : null;

  // Primary band/mode from prefer-epic sniper when SB is the confidence source.
  let confidenceBand = primaryAccount.band;
  let confidencePct = primaryAccount.pct;
  let setupMode = primaryAccount.mode;
  let confidenceSource = primaryAccount.suppressed
    ? null
    : primaryAccount.source;
  if (preferSb && preferEpic && !primaryAccount.suppressed) {
    const prefRow = marketRows.find((r) => r.epic === preferEpic);
    if (prefRow) {
      confidencePct = prefRow.pct;
      setupMode = prefRow.mode;
      const thr = 0.68;
      if (prefRow.approved && prefRow.pct >= thr) confidenceBand = "High";
      else if (prefRow.pct >= thr * 0.85) confidenceBand = "Med";
      else confidenceBand = "Low";
      confidenceSource = "SB macro";
    }
  }

  return {
    engines: [cfdEngine, sbEngine],
    focusMarket: shortMarketLabel(focusEpic),
    focusEpic,
    promotedMarkets,
    rankedActive: ranked?.active === true,
    confidenceBand,
    confidencePct,
    confidenceSource,
    setupMode,
    confidenceAccounts,
    marketHierarchy,
    preferEpic,
    preferMarket: preferEpic ? shortMarketLabel(preferEpic) : null,
    preferenceReason,
    rotationKind: rotation.kind,
    rotationLabel: rotation.label,
    nextBlock,
    sbPostureHint:
      sbEngine.state === "ARMED" ? SB_DUAL_REGIME_HINT : null,
  };
}

/** Prefer-epic sniper row for SB account line; falls back to ops_strip sniper. */
export function resolveSniperForPreferEpic(args: {
  ops: DeskIntentOpsSlice | null;
  sniperByEpic?: DeskIntentSniperByEpic | null;
  preferEpic: string | null;
  focusEpic: string | null;
}): DeskIntentOpsSlice["sniper_ml"] {
  const epic = args.preferEpic || args.focusEpic;
  if (epic && args.sniperByEpic?.by_epic?.[epic]) {
    const row = args.sniperByEpic.by_epic[epic];
    return {
      p_success: row.p_success,
      approved: row.approved,
      threshold: row.threshold,
      epic,
    };
  }
  return args.ops?.sniper_ml ?? null;
}
