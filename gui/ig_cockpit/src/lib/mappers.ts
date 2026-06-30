import { regimeAccent } from "./regimeTheme";
import { feedHealthLevel, latencyColor, latencyTracker } from "./metrics";
import type {
  BootStatusView,
  BootStepStatus,
  CandlePoint,
  ChartViewModel,
  EnvelopeState,
  GovernanceFlag,
  JsonObject,
  LogLine,
  OrderValveState,
  PricePoint,
  RiskBand,
  RiskItem,
  RouteRow,
  RouteState,
  SignalMarker,
  StatusLevel,
  StatusRow,
  TopBarView,
  WsConnectionState,
} from "../types/cockpit";

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function asNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function asString(value: unknown, fallback = ""): string {
  if (value === null || value === undefined) return fallback;
  return String(value);
}

function levelFromBool(ok: boolean, warn = false): StatusLevel {
  if (ok) return "ok";
  if (warn) return "warn";
  return "error";
}

const NIGHT_MATRIX_EPICS = 4;

const REQUIRED_GUI_FIELDS = [
  "strategy_selector_advice",
  "strategy_controller_decisions",
  "strategy_governance",
  "unified_execution_route",
  "hard_enforcement_decisions",
  "trade_pipeline_health",
  "regime_detection",
  "regime_risk_envelope",
  "daily_pnl_targeting",
] as const;

export function isGuiReady(gui: JsonObject | null): boolean {
  if (!gui) return false;
  if (gui.gui_attach_ready === true) return true;
  const status = asString(gui.session_status).toUpperCase();
  return status === "HEALTHY" && !!gui.session_id;
}

export function isGuiFullyReady(gui: JsonObject | null): boolean {
  if (!isGuiReady(gui)) return false;
  return REQUIRED_GUI_FIELDS.every((f) => f in (gui as JsonObject));
}

/** Partial readiness — cockpit can operate while advisory chain warms. */
export function isCockpitUsable(
  gui: JsonObject | null,
  agentState: JsonObject | null = null,
): boolean {
  if (gui?.cockpit_usable === true) return true;
  if (agentState?.cockpit_usable === true) return true;
  const level = asNumber(gui?.readiness_level) ?? asNumber(agentState?.readiness_level);
  if (level !== null && level >= 1) {
    const subs = asObject(gui?.subsystem_readiness ?? agentState?.subsystem_readiness);
    const feeds = asObject(subs.feeds);
    if (feeds.ready === true || feeds.level === "partial" || feeds.level === "ready") {
      return true;
    }
    if (level >= 2) return true;
  }
  return isGuiReady(gui);
}

export function readinessLabel(gui: JsonObject | null): string {
  return asString(gui?.readiness_label, "Connecting…");
}

export function resolveOrderValve(
  gui: JsonObject | null,
  tick: JsonObject | null,
): OrderValveState {
  const hard = asArray<JsonObject>(gui?.hard_enforcement_decisions).some(
    (h) => h.active,
  );
  if (hard || tick?.trading_paused === true) return "suppressed";
  const routes = asArray<JsonObject>(gui?.unified_execution_route);
  if (routes.length === 0) return "warming";
  return "open";
}

export function mapTopBar(
  gui: JsonObject | null,
  pnl: JsonObject | null,
  tick: JsonObject | null,
  wsState: WsConnectionState,
  tickLatencyMs: number | null,
): TopBarView {
  const regimeRows = asArray<JsonObject>(gui?.regime_detection);
  const regime = asString(
    regimeRows[0]?.regime ?? regimeRows[0]?.dominant_regime,
    "Unknown",
  );
  const selector = asArray<JsonObject>(gui?.strategy_selector_advice);
  const strategy = asString(
    selector[0]?.recommended_strategy ?? selector[0]?.strategy,
    "—",
  );
  const pnlGbp =
    asNumber(tick?.daily_pnl_gbp) ??
    asNumber(pnl?.daily_pnl_gbp) ??
    asNumber(asObject(pnl?.daily_pnl_targeting).current_points);

  const feeds = asArray<JsonObject>(gui?.api_feed_health);
  const fresh = feeds.filter((f) => f.fresh === true).length;
  const feedStatus =
    feeds.length > 0
      ? `${fresh}/${feeds.length} fresh`
      : asString(tick?.stream_status, "offline");

  return {
    regime,
    regimeColor: regimeAccent(regime),
    strategy,
    pnlGbp,
    sessionStatus: asString(gui?.session_status, "UNKNOWN"),
    wsConnected: wsState === "connected",
    wsState,
    feedStatus,
    feedHealth: feedHealthLevel(fresh, feeds.length),
    latencyMs: tickLatencyMs,
    latencyLevel: latencyColor(tickLatencyMs),
    tradingPaused: tick?.trading_paused === true,
    orderValve: resolveOrderValve(gui, tick),
  };
}

export function mapStatusMatrix(
  gui: JsonObject | null,
  tick: JsonObject | null,
  lastUpdated: string | null,
): StatusRow[] {
  const feeds = asArray<JsonObject>(gui?.api_feed_health);
  const freshFeeds = feeds.filter((f) => f.fresh === true);
  const routes = asArray<JsonObject>(gui?.unified_execution_route);
  const pipeline = asArray<JsonObject>(gui?.trade_pipeline_health);
  const governance = asObject(gui?.pipeline_governance);
  const hard = asArray<JsonObject>(gui?.hard_enforcement_decisions).filter(
    (h) => h.active,
  );
  const sessionOk = isGuiReady(gui);
  const streamStatus = asString(tick?.stream_status, "DISCONNECTED");
  const routeProgress = Math.min(
    100,
    Math.round((routes.length / NIGHT_MATRIX_EPICS) * 100),
  );
  const activeRoutes = routes.filter(
    (r) => asString(r.execution_path, "NONE") !== "NONE",
  );
  const valve = resolveOrderValve(gui, tick);
  const valveHint =
    valve === "suppressed"
      ? hard[0]
        ? asString(hard[0].reason, "Hard enforcement active")
        : "Order valve suppressed — trading paused or enforcement active"
      : valve === "warming"
        ? "Unified routes warming — cache populating"
        : "Order valve open";

  const updated = lastUpdated ?? asString(gui?.ts);

  return [
    {
      key: "auth",
      label: "Authentication",
      status: levelFromBool(sessionOk, !sessionOk && !!gui?.session_id),
      detail: asString(gui?.session_status, "No session"),
      hint: gui?.session_id ? `Scope ${asString(gui.account_scope, "—")}` : undefined,
      updatedAt: updated,
    },
    {
      key: "feeds",
      label: "Data Feeds",
      status:
        feeds.length === 0
          ? "warn"
          : freshFeeds.length === feeds.length
            ? "ok"
            : freshFeeds.length > 0
              ? "warn"
              : "error",
      detail: `${freshFeeds.length}/${feeds.length || "?"} fresh · ${streamStatus}`,
      progress: feeds.length ? Math.round((freshFeeds.length / feeds.length) * 100) : 0,
      updatedAt: updated,
    },
    {
      key: "routing",
      label: "Routing",
      status:
        routes.length >= NIGHT_MATRIX_EPICS
          ? "ok"
          : routes.length > 0
            ? "warn"
            : "error",
      detail:
        routes.length > 0
          ? `${activeRoutes.length}/${routes.length} armed · warm ${routeProgress}%`
          : "Awaiting warm-up",
      progress: routeProgress,
      hint: "Unified execution route cache",
      updatedAt: updated,
    },
    {
      key: "execution",
      label: "Execution",
      status: levelFromBool(
        pipeline.some((p) => asString(p.pipeline_state) === "LIVE"),
        pipeline.some((p) => p.order_prepared),
      ),
      detail: pipeline.length
        ? `${pipeline.filter((p) => p.ready !== false).length}/${pipeline.length} pipelines`
        : "Flat",
      hint: valveHint,
      updatedAt: updated,
    },
    {
      key: "risk",
      label: "Risk",
      status: levelFromBool(
        hard.length === 0,
        hard.length > 0,
      ),
      detail: asString(
        asArray<JsonObject>(gui?.regime_risk_envelope)[0]?.risk_profile,
        "Within limits",
      ),
      hint: asString(governance.risk_posture, "Governance nominal"),
      updatedAt: updated,
    },
    {
      key: "ledger",
      label: "Ledger",
      status: levelFromBool(!!gui?.session_id, !gui?.session_id),
      detail: gui?.session_id
        ? `Session ${asString(gui.session_id).slice(0, 8)}…`
        : "Not attached",
      updatedAt: updated,
    },
  ];
}

function routeStateFromRow(
  row: JsonObject,
  pipeline: JsonObject | undefined,
  feed: JsonObject | undefined,
): RouteState {
  const path = asString(row.execution_path, "NONE");
  if (path === "NONE") return "idle";
  const flags = asArray<string>(row.route_flags);
  if (flags.some((f) => /degrad|block|suppress/i.test(f))) return "degraded";
  if (!pipeline && !feed) return "warming";
  if (feed && feed.fresh !== true) return "degraded";
  return "active";
}

export function mapRoutingPanel(
  routing: JsonObject | null,
  gui: JsonObject | null,
): RouteRow[] {
  const routes = asArray<JsonObject>(routing?.unified_execution_route);
  const pipeline = asArray<JsonObject>(gui?.trade_pipeline_health);
  const feeds = asArray<JsonObject>(gui?.api_feed_health);

  return routes.map((row) => {
    const epic = asString(row.epic, "—");
    const pipe = pipeline.find((p) => asString(p.epic) === epic);
    const feed = feeds.find((f) => asString(f.epic) === epic);
    const path = asString(row.execution_path, "NONE");
    const spread = asNumber(feed?.spread);
    const age = asNumber(feed?.age_s);
    const fillPrice = asNumber(pipe?.fill_price);
    const lastPrice = asNumber(pipe?.last_price);

    const rawLatency = age !== null ? Math.round(age * 1000) : null;
    const smoothLatency = latencyTracker.smoothLatency(epic, rawLatency);
    const slippageBps =
      fillPrice !== null && lastPrice !== null && lastPrice > 0
        ? Number((Math.abs(((fillPrice - lastPrice) / lastPrice) * 10000)).toFixed(1))
        : spread !== null
          ? Number((spread * 100).toFixed(1))
          : null;

    return {
      epic,
      venue: "IG Demo",
      path,
      state: routeStateFromRow(row, pipe, feed),
      confidence: asNumber(row.route_confidence) ?? 0,
      latencyMs: smoothLatency,
      fillQuality: pipe?.order_confirmed ? "filled" : pipe?.order_dispatched ? "pending" : "idle",
      slippageBps,
      slippageTrend: latencyTracker.slippageTrend(epic, slippageBps),
      reason: asString(row.route_reason, "—"),
      flags: asArray<string>(row.route_flags),
    };
  });
}

function envelopeState(gui: JsonObject | null, risk: JsonObject | null): EnvelopeState {
  const hard = asArray<JsonObject>(risk?.hard_enforcement_decisions ?? gui?.hard_enforcement_decisions).filter(
    (h) => h.active,
  );
  if (hard.length > 0) return "breached";
  const envelope = asArray<JsonObject>(risk?.regime_risk_envelope);
  if (envelope.length === 0) return "disabled";
  return "active";
}

export function mapRiskPanel(
  risk: JsonObject | null,
  gui: JsonObject | null,
  tick: JsonObject | null,
): {
  envelopeState: EnvelopeState;
  orderValve: OrderValveState;
  items: RiskItem[];
  flags: GovernanceFlag[];
  alerts: string[];
} {
  const targeting = asObject(risk?.daily_pnl_targeting);
  const governance = asObject(risk?.session_governance);
  const pipeGov = asObject(risk?.pipeline_governance);
  const hard = asArray<JsonObject>(risk?.hard_enforcement_decisions).filter(
    (h) => h.active,
  );
  const stratRows = asArray<JsonObject>(gui?.strategy_governance);
  const stratGov = stratRows[0] ?? asObject(gui?.strategy_governance);
  const positions = asArray<JsonObject>(tick?.positions);
  const envState = envelopeState(gui, risk);
  const valve = resolveOrderValve(gui, tick);

  const items: RiskItem[] = [
    {
      label: "Risk envelope",
      value: envState.toUpperCase(),
      status: envState === "active" ? "ok" : envState === "breached" ? "error" : "warn",
      explanation:
        envState === "breached"
          ? "Hard enforcement or envelope breach — routes blocked"
          : envState === "disabled"
            ? "Envelope not yet populated from gui_status"
            : "Regime risk envelope advising sizing and route bias",
    },
    {
      label: "Order valve",
      value: valve.toUpperCase(),
      status: valve === "open" ? "ok" : valve === "warming" ? "warn" : "error",
      explanation:
        valve === "suppressed"
          ? hard[0]
            ? asString(hard[0].reason, "Orders suppressed by enforcement")
            : "Trading paused or REST budget guard"
          : valve === "warming"
            ? "Waiting for unified route warm-up at boot"
            : "Orders permitted through pipeline",
    },
    {
      label: "Open positions",
      value: `${positions.length}`,
      status: levelFromBool(positions.length < 4, positions.length >= 4),
      explanation: "Live position count vs typical max (4 night matrix)",
    },
    {
      label: "Daily progress",
      value: `${Math.round((asNumber(targeting.progress_ratio) ?? 0) * 100)}% · ${asString(targeting.progress_band, "—")}`,
      status: levelFromBool(asString(targeting.recommended_bias) !== "TIGHTEN"),
      explanation: asString(
        targeting.recommended_bias ?? targeting.progress_band,
        "Daily P&L targeting advisory",
      ),
    },
    {
      label: "Session risk score",
      value: asString(governance.session_risk_score ?? targeting.session_risk_score, "—"),
      status: levelFromBool((asNumber(governance.session_risk_score) ?? 50) < 70, true),
      explanation: asString(pipeGov.risk_posture, "Pipeline governance posture"),
    },
  ];

  const flags: GovernanceFlag[] = asArray<string>(stratGov.governance_flags).map((f) => ({
    label: f,
    active: true,
    detail: asString(stratGov.governance_reason, "strategy governance"),
  }));

  if (flags.length === 0 && stratGov.governance_reason) {
    flags.push({
      label: "Governance",
      active: true,
      detail: asString(stratGov.governance_reason),
    });
  }

  const alerts = hard.map(
    (h) =>
      `${asString(h.epic, "global")}: ${asString(h.reason, "hard enforcement")}`,
  );

  return { envelopeState: envState, orderValve: valve, items, flags, alerts };
}

export function mapLogsFromRest(
  logs: JsonObject | null,
  tick: JsonObject | null,
): LogLine[] {
  const lines: LogLine[] = [];
  let seq = 0;
  const mk = (
    ts: string,
    message: string,
    level: LogLine["level"],
    subsystem: LogLine["subsystem"],
  ): LogLine => ({
    id: `rest-${seq++}`,
    ts,
    message,
    level,
    subsystem,
  });

  for (const sig of asArray<JsonObject>(logs?.signals).slice(0, 30)) {
    lines.push(
      mk(
        asString(sig.ts ?? sig.timestamp, "—"),
        `${asString(sig.epic, "?")} ${asString(sig.direction ?? sig.signal)} conf=${asString(sig.confidence)}`,
        "info",
        "execution",
      ),
    );
  }

  const summary = asString(logs?.health_summary);
  if (summary) {
    lines.unshift(
      mk(asString(logs?.ts ?? tick?.ts, "—"), summary, "info", "feeds"),
    );
  }

  return lines;
}

export function extractPriceFromTick(
  tick: JsonObject | null,
  gui: JsonObject | null,
): { epic: string; bid: number | null; offer: number | null; mid: number | null } {
  const markets = asObject(tick?.markets);
  const epic =
    Object.keys(markets)[0] ??
    asString(asArray<JsonObject>(gui?.api_feed_health)[0]?.epic, "—");

  let bid = asNumber(tick?.bid);
  let offer = asNumber(tick?.offer);

  if ((!bid || !offer) && epic && markets[epic]) {
    const slice = asObject(markets[epic]);
    bid = asNumber(slice.bid) ?? bid;
    offer = asNumber(slice.offer) ?? offer;
  }

  const mid =
    bid !== null && offer !== null ? Number(((bid + offer) / 2).toFixed(5)) : null;

  return { epic, bid, offer, mid };
}

export function appendPricePoint(
  history: PricePoint[],
  tick: JsonObject | null,
  maxPoints = 300,
): PricePoint[] {
  const { mid } = extractPriceFromTick(tick, null);
  if (mid === null) return history;
  const ts = tick?.ts ? Date.parse(asString(tick.ts)) : Date.now();
  const time = Math.floor(ts / 1000);
  if (history.length && history[history.length - 1].time === time) {
    return [...history.slice(0, -1), { time, value: mid }];
  }
  const next = [...history, { time, value: mid }];
  return next.length > maxPoints ? next.slice(-maxPoints) : next;
}

export function appendPnLPoint(
  history: PricePoint[],
  tick: JsonObject | null,
  pnl: JsonObject | null,
  maxPoints = 300,
): PricePoint[] {
  const value =
    asNumber(tick?.daily_pnl_gbp) ??
    asNumber(asObject(pnl?.daily_pnl_targeting).current_points);
  if (value === null) return history;
  const ts = tick?.ts ? Date.parse(asString(tick.ts)) : Date.now();
  const time = Math.floor(ts / 1000);
  if (history.length && history[history.length - 1].time === time) {
    return [...history.slice(0, -1), { time, value }];
  }
  const next = [...history, { time, value }];
  return next.length > maxPoints ? next.slice(-maxPoints) : next;
}

export function tickToCandle(
  tick: JsonObject | null,
  history: CandlePoint[],
  maxPoints = 120,
): CandlePoint[] {
  const { mid } = extractPriceFromTick(tick, null);
  if (mid === null) return history;
  const ts = tick?.ts ? Date.parse(asString(tick.ts)) : Date.now();
  const time = Math.floor(ts / 60) * 60;
  const last = history[history.length - 1];
  if (last && last.time === time) {
    return [
      ...history.slice(0, -1),
      {
        time,
        open: last.open,
        high: Math.max(last.high, mid),
        low: Math.min(last.low, mid),
        close: mid,
      },
    ];
  }
  return [...history, { time, open: mid, high: mid, low: mid, close: mid }].slice(
    -maxPoints,
  );
}

export function buildSignalMarkers(
  tick: JsonObject | null,
  prev: JsonObject | null,
  existing: SignalMarker[],
): SignalMarker[] {
  if (!tick) return existing;
  const signal = asObject(tick.signal);
  const direction = asString(signal.direction).toUpperCase();
  const prevDir = asString(asObject(prev?.signal).direction).toUpperCase();
  if (!direction || direction === "WAIT" || direction === prevDir) return existing;

  const ts = tick.ts ? Date.parse(asString(tick.ts)) : Date.now();
  const time = Math.floor(ts / 60) * 60;
  const isBuy = direction.includes("BUY") || direction.includes("LONG");
  const marker: SignalMarker = {
    time,
    position: isBuy ? "belowBar" : "aboveBar",
    color: isBuy ? "#34d399" : "#f87171",
    shape: isBuy ? "arrowUp" : "arrowDown",
    text: `${direction} ${asString(signal.confidence)}%`,
  };
  return [...existing, marker].slice(-40);
}

export function buildRiskBands(gui: JsonObject | null, pnl: JsonObject | null): RiskBand[] {
  const targeting = asObject(pnl?.daily_pnl_targeting ?? gui?.daily_pnl_targeting);
  const target = asNumber(targeting.target_points);
  const current = asNumber(targeting.current_points);
  if (target === null) return [];
  const bands: RiskBand[] = [
    { upper: target, lower: target * 0.85, label: "Target band" },
  ];
  if (current !== null) {
    bands.push({ upper: current, lower: current, label: "Current" });
  }
  return bands;
}

export function mapChartView(
  gui: JsonObject | null,
  pnl: JsonObject | null,
  tick: JsonObject | null,
  candleHistory: CandlePoint[],
  pnlHistory: PricePoint[],
  markers: SignalMarker[],
): ChartViewModel {
  const regimeRows = asArray<JsonObject>(gui?.regime_detection);
  const regime = asString(
    regimeRows[0]?.regime ?? regimeRows[0]?.dominant_regime,
    "Unknown",
  );
  const { epic } = extractPriceFromTick(tick, gui);
  const targeting = asObject(pnl?.daily_pnl_targeting ?? gui?.daily_pnl_targeting);

  return {
    epic,
    regime,
    regimeColor: regimeAccent(regime),
    candleHistory,
    pnlHistory,
    markers,
    riskBands: buildRiskBands(gui, pnl),
    targetPnl: asNumber(targeting.target_points),
    currentPnl:
      asNumber(tick?.daily_pnl_gbp) ?? asNumber(targeting.current_points),
  };
}

export function mapBootStatus(raw: JsonObject | null): BootStatusView {
  const empty: BootStatusView = {
    tradeReady: false,
    currentStage: "A",
    bootElapsedSec: 0,
    estimatedReadySec: null,
    stages: [],
    subsystems: [],
    blockers: [],
  };
  if (!raw) return empty;
  const stages = asArray<JsonObject>(raw.stages).map((s) => ({
    id: asString(s.id),
    label: asString(s.label),
    status: asString(s.status, "pending") as BootStepStatus,
    lastError: asString(s.last_error),
    retryCount: asNumber(s.retry_count) ?? 0,
    elapsedMs: asNumber(s.elapsed_ms),
  }));
  const subsystems = asArray<JsonObject>(raw.subsystems).map((s) => ({
    id: asString(s.id),
    label: asString(s.label),
    status: asString(s.status, "pending") as BootStepStatus,
    lastError: asString(s.last_error),
    retryCount: asNumber(s.retry_count) ?? 0,
    healAction: asString(s.heal_action),
  }));
  const diagRaw = raw.startup_diagnostics as JsonObject | undefined;
  const diagLabels: Record<string, string> = {
    size_rules_loaded: "IG size rules",
    trailing_stop_engine_active: "Trailing stops",
    dynamic_limit_engine_active: "Dynamic limits",
    execution_loop_ready: "Execution loop",
    ig_connectivity_validated: "IG connectivity",
    rotation_logic_active: "Market rotation",
    feed_heartbeat_live: "Feed heartbeat",
    routing_armed: "Routing armed",
  };
  const startupDiagnostics = diagRaw
    ? Object.entries(diagLabels).map(([key, label]) => ({
        key,
        label,
        ok: Boolean(diagRaw[key]),
      }))
    : undefined;
  return {
    tradeReady: Boolean(raw.trade_ready),
    currentStage: asString(raw.current_stage, "A"),
    bootElapsedSec: asNumber(raw.boot_elapsed_sec) ?? 0,
    estimatedReadySec: asNumber(raw.estimated_ready_sec),
    stages,
    subsystems,
    blockers: asArray<string>(raw.blockers).map((b) => String(b)),
    startupDiagnostics,
  };
}
