import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { listen } from "@tauri-apps/api/event";
import {
  getAgentState,
  getGuiStatus,
  getLogs,
  getPnlData,
  getRiskState,
  getRoutingMetrics,
  fetchBootStatus,
  isTauri,
  pauseTrading,
  resumeTrading,
  suppressOrders,
  wsStateUrl,
  wsStreamUrl,
} from "../lib/api";
import { mergeAgentStateIntoGui } from "../lib/agentState";
import { debounce, mergeIfNewer, parsePayloadTs } from "../lib/coherence";
import { mergeLogLines } from "../lib/logStream";
import {
  isGuiFullyReady,
  mapChartView,
  mapLogsFromRest,
  mapRiskPanel,
  mapRoutingPanel,
  mapStatusMatrix,
  mapTopBar,
  isCockpitUsable,
  mapBootStatus,
  readinessLabel,
} from "../lib/mappers";
import { processTickBatch, TickBatcher } from "../lib/tickProcessor";
import { ResilientWebSocket } from "../lib/wsClient";
import type {
  BootStatusView,
  CandlePoint,
  ChartViewModel,
  GovernanceFlag,
  JsonObject,
  LogLine,
  LogSubsystem,
  PanelFocus,
  PricePoint,
  RiskItem,
  RouteRow,
  SignalMarker,
  StatusRow,
  TopBarView,
  WsConnectionState,
} from "../types/cockpit";
import type { EnvelopeState, OrderValveState } from "../types/cockpit";

const POLL_MS = 30_000;
/** When stream + state WS are live, gui_status is advisory-only — poll less aggressively. */
const POLL_MS_WS_CONNECTED = 60_000;
const GUI_REFRESH_ON_TICK_MS = 8_000;
const REST_DEBOUNCE_MS = 350;
const SPLASH_MAX_MS = 45_000;
const BOOT_POLL_MS = 1_500;

interface CockpitContextValue {
  guiStatus: JsonObject | null;
  agentState: JsonObject | null;
  tick: JsonObject | null;
  wsConnected: boolean;
  wsState: WsConnectionState;
  loading: boolean;
  ready: boolean;
  cockpitUsable: boolean;
  readinessLabel: string;
  showSplash: boolean;
  bootStatus: BootStatusView | null;
  cockpitFailed: boolean;
  errors: string[];
  lastUpdated: string | null;
  tickLatencyMs: number | null;
  topBar: TopBarView;
  statusRows: StatusRow[];
  routes: RouteRow[];
  riskItems: RiskItem[];
  riskAlerts: string[];
  riskFlags: GovernanceFlag[];
  envelopeState: EnvelopeState;
  orderValve: OrderValveState;
  logLines: LogLine[];
  logFilter: LogSubsystem;
  logsPaused: boolean;
  chart: ChartViewModel;
  panelFocus: PanelFocus;
  refresh: () => void;
  dismissError: (index: number) => void;
  setLogFilter: (f: LogSubsystem) => void;
  setLogsPaused: (v: boolean) => void;
  setPanelFocus: (p: PanelFocus) => void;
  pauseTrading: () => void;
  resumeTrading: () => void;
  suppressOrders: () => void;
  openBrowserFallback: () => void;
}

const CockpitContext = createContext<CockpitContextValue | null>(null);

function pushError(errors: string[], message: string): string[] {
  if (errors.includes(message)) return errors;
  return [...errors.slice(-4), message];
}

export function CockpitProvider({ children }: { children: ReactNode }) {
  const [guiStatus, setGuiStatus] = useState<JsonObject | null>(null);
  const [agentState, setAgentState] = useState<JsonObject | null>(null);
  const [pnl, setPnl] = useState<JsonObject | null>(null);
  const [routing, setRouting] = useState<JsonObject | null>(null);
  const [risk, setRisk] = useState<JsonObject | null>(null);
  const [logsRest, setLogsRest] = useState<JsonObject | null>(null);
  const [tick, setTick] = useState<JsonObject | null>(null);
  const [wsState, setWsState] = useState<WsConnectionState>("disconnected");
  const [stateWsConnected, setStateWsConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [tickLatencyMs, setTickLatencyMs] = useState<number | null>(null);
  const [candleHistory, setCandleHistory] = useState<CandlePoint[]>([]);
  const [pnlHistory, setPnlHistory] = useState<PricePoint[]>([]);
  const [markers, setMarkers] = useState<SignalMarker[]>([]);
  const [liveLogs, setLiveLogs] = useState<LogLine[]>([]);
  const [logFilter, setLogFilter] = useState<LogSubsystem>("all");
  const [logsPaused, setLogsPaused] = useState(false);
  const [panelFocus, setPanelFocus] = useState<PanelFocus>(null);
  const [showSplash, setShowSplash] = useState(true);
  const [bootStatus, setBootStatus] = useState<BootStatusView | null>(null);
  const [cockpitFailed, setCockpitFailed] = useState(false);

  const pnlRef = useRef<JsonObject | null>(null);
  const prevTickRef = useRef<JsonObject | null>(null);
  const wsTickTsRef = useRef(0);
  const lastGuiRefreshRef = useRef(0);
  const logsPausedRef = useRef(false);
  const chartAccRef = useRef({
    candleHistory: [] as CandlePoint[],
    pnlHistory: [] as PricePoint[],
    markers: [] as SignalMarker[],
  });

  pnlRef.current = pnl;
  logsPausedRef.current = logsPaused;

  const cockpitView = useMemo(
    () => mergeAgentStateIntoGui(guiStatus, agentState),
    [guiStatus, agentState],
  );
  const ready = isGuiFullyReady(guiStatus);
  const cockpitUsable = isCockpitUsable(cockpitView, agentState);
  const readinessLabelText = readinessLabel(cockpitView);
  const wsConnected = wsState === "connected";

  const applyRest = useCallback((index: number, value: JsonObject) => {
    const wsTs = wsTickTsRef.current;
    switch (index) {
      case 0:
        setGuiStatus((c) => mergeIfNewer(c, value, wsTs));
        break;
      case 1:
        setPnl((c) => mergeIfNewer(c, value, wsTs));
        break;
      case 2:
        setRouting((c) => mergeIfNewer(c, value, wsTs));
        break;
      case 3:
        setRisk((c) => mergeIfNewer(c, value, wsTs));
        break;
      case 4:
        setLogsRest((c) => mergeIfNewer(c, value, wsTs));
        break;
      default:
        break;
    }
  }, []);

  const loadRest = useCallback(async (silent = false) => {
    const skipAgentStateRest = stateWsConnected;
    const tasks: Promise<JsonObject>[] = [
      getGuiStatus(),
      getPnlData(),
      getRoutingMetrics(),
      getRiskState(),
      getLogs(),
    ];
    if (!skipAgentStateRest) {
      tasks.push(getAgentState());
    }
    const results = await Promise.allSettled(tasks);

    const labels = skipAgentStateRest
      ? ["gui_status", "pnl", "routing", "risk", "logs"]
      : ["gui_status", "pnl", "routing", "risk", "logs", "agent_state"];
    const nextErrors: string[] = [];

    results.forEach((result, index) => {
      if (result.status === "fulfilled") {
        const agentIdx = skipAgentStateRest ? -1 : 5;
        if (index === agentIdx) {
          setAgentState((c) => mergeIfNewer(c, result.value, wsTickTsRef.current));
        } else {
          applyRest(index, result.value);
        }
      } else if (!silent) {
        nextErrors.push(`${labels[index]}: ${result.reason?.message ?? "failed"}`);
      }
    });

    if (nextErrors.length) {
      setErrors((prev) => nextErrors.reduce(pushError, prev));
    }
    setLastUpdated(new Date().toISOString());
    setLoading(false);
  }, [applyRest, stateWsConnected]);

  const debouncedLoadRest = useMemo(
    () => debounce(() => void loadRest(true), REST_DEBOUNCE_MS),
    [loadRest],
  );

  const refresh = useCallback(() => {
    void loadRest();
  }, [loadRest]);

  const dismissError = useCallback((index: number) => {
    setErrors((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const openBrowserFallback = useCallback(() => {
    const base = import.meta.env.VITE_IG_AGENT_API_URL ?? "http://127.0.0.1:8080";
    window.open(base, "_blank");
  }, []);

  const flushTickBatch = useCallback(
    (batch: JsonObject[]) => {
      if (!batch.length) return;
      const latest = batch[batch.length - 1];
      const received = Date.now();
      const tickTs = parsePayloadTs(latest) || received;
      wsTickTsRef.current = Math.max(wsTickTsRef.current, tickTs);
      setTickLatencyMs(Math.max(0, received - tickTs));

      const result = processTickBatch(
        batch,
        chartAccRef.current,
        prevTickRef.current,
        pnlRef.current,
      );
      chartAccRef.current = result.acc;
      prevTickRef.current = result.prevTick;

      setTick(latest);
      setCandleHistory(result.acc.candleHistory);
      setPnlHistory(result.acc.pnlHistory);
      setMarkers(result.acc.markers);

      if (!logsPausedRef.current && result.logs.length) {
        setLiveLogs((lines) => mergeLogLines(lines, result.logs));
      }

      const now = Date.now();
      if (now - lastGuiRefreshRef.current > GUI_REFRESH_ON_TICK_MS) {
        lastGuiRefreshRef.current = now;
        debouncedLoadRest();
      }
    },
    [debouncedLoadRest],
  );

  useEffect(() => {
    void loadRest();
    const pollMs =
      wsConnected && stateWsConnected ? POLL_MS_WS_CONNECTED : POLL_MS;
    const id = window.setInterval(() => void loadRest(true), pollMs);
    return () => {
      window.clearInterval(id);
      debouncedLoadRest.cancel();
    };
  }, [loadRest, debouncedLoadRest, wsConnected, stateWsConnected]);

  useEffect(() => {
    if (!showSplash) return undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const raw = await fetchBootStatus();
        if (!cancelled) setBootStatus(mapBootStatus(raw));
      } catch {
        /* boot_status optional during earliest bind */
      }
    };
    void poll();
    const id = window.setInterval(() => void poll(), BOOT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [showSplash]);

  useEffect(() => {
    if (bootStatus?.tradeReady && !loading && wsConnected) {
      const t = window.setTimeout(() => setShowSplash(false), 600);
      return () => window.clearTimeout(t);
    }
    if (cockpitUsable && !loading && wsConnected) {
      const t = window.setTimeout(() => setShowSplash(false), ready ? 800 : 400);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [bootStatus?.tradeReady, cockpitUsable, ready, loading, wsConnected]);

  useEffect(() => {
    const t = window.setTimeout(() => {
      if (showSplash) {
        setCockpitFailed(true);
        setShowSplash(false);
        setErrors((prev) =>
          pushError(prev, "Cockpit boot timeout — use browser fallback if needed"),
        );
      }
    }, SPLASH_MAX_MS);
    return () => window.clearTimeout(t);
  }, [showSplash]);

  useEffect(() => {
    const batcher = new TickBatcher(flushTickBatch);

    const onWsStatus = (state: WsConnectionState) => {
      setWsState(state);
    };

    if (isTauri()) {
      let unlistenTick: (() => void) | undefined;
      let unlistenConn: (() => void) | undefined;

      void listen<JsonObject>("ws-stream", (event) => {
        batcher.push(event.payload);
      }).then((fn) => {
        unlistenTick = fn;
      });

      void listen<JsonObject>("ws-connection", (event) => {
        const state = String(event.payload.state ?? "");
        if (state === "connected" || state === "reconnecting" || state === "degraded" || state === "disconnected") {
          onWsStatus(state as WsConnectionState);
        } else {
          onWsStatus(event.payload.connected ? "connected" : "reconnecting");
        }
        if (event.payload.error) {
          setErrors((prev) =>
            pushError(prev, `WebSocket: ${String(event.payload.error)}`),
          );
        }
      }).then((fn) => {
        unlistenConn = fn;
      });

      return () => {
        unlistenTick?.();
        unlistenConn?.();
        batcher.dispose();
      };
    }

    const client = new ResilientWebSocket(
      wsStreamUrl(),
      (payload) => batcher.push(payload),
      onWsStatus,
    );
    client.connect();

    return () => {
      client.dispose();
      batcher.dispose();
    };
  }, [flushTickBatch]);

  useEffect(() => {
    if (isTauri()) return undefined;

    const client = new ResilientWebSocket(
      wsStateUrl(),
      (payload) => {
        setAgentState((c) => mergeIfNewer(c, payload, wsTickTsRef.current));
        setLastUpdated(new Date().toISOString());
      },
      (state) => {
        setStateWsConnected(state === "connected");
      },
    );
    client.connect();

    return () => {
      client.dispose();
      setStateWsConnected(false);
    };
  }, []);

  const restLogs = useMemo(
    () => mapLogsFromRest(logsRest, tick),
    [logsRest, tick],
  );

  const allLogs = useMemo(() => {
    const combined = [...restLogs, ...liveLogs];
    combined.sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
    const seen = new Set<string>();
    return combined.filter((l) => {
      if (seen.has(l.id)) return false;
      seen.add(l.id);
      return true;
    });
  }, [restLogs, liveLogs]);

  const logLines = useMemo(() => {
    if (logFilter === "all") return allLogs;
    return allLogs.filter((l) => l.subsystem === logFilter);
  }, [allLogs, logFilter]);

  const topBar = useMemo(
    () => mapTopBar(cockpitView, pnl, tick, wsState, tickLatencyMs),
    [cockpitView, pnl, tick, wsState, tickLatencyMs],
  );
  const statusRows = useMemo(
    () => mapStatusMatrix(cockpitView, tick, lastUpdated),
    [cockpitView, tick, lastUpdated],
  );
  const routes = useMemo(
    () => mapRoutingPanel(routing, cockpitView),
    [routing, cockpitView],
  );
  const riskView = useMemo(
    () => mapRiskPanel(risk, cockpitView, tick),
    [risk, cockpitView, tick],
  );
  const chart = useMemo(
    () => mapChartView(cockpitView, pnl, tick, candleHistory, pnlHistory, markers),
    [cockpitView, pnl, tick, candleHistory, pnlHistory, markers],
  );

  const value: CockpitContextValue = {
    guiStatus: cockpitView,
    agentState,
    tick,
    wsConnected,
    wsState,
    loading,
    ready,
    cockpitUsable,
    readinessLabel: readinessLabelText,
    showSplash,
    bootStatus,
    cockpitFailed,
    errors,
    lastUpdated,
    tickLatencyMs,
    topBar,
    statusRows,
    routes,
    riskItems: riskView.items,
    riskAlerts: riskView.alerts,
    riskFlags: riskView.flags,
    envelopeState: riskView.envelopeState,
    orderValve: riskView.orderValve,
    logLines,
    logFilter,
    logsPaused,
    chart,
    panelFocus,
    refresh,
    dismissError,
    setLogFilter,
    setLogsPaused,
    setPanelFocus,
    pauseTrading,
    resumeTrading,
    suppressOrders,
    openBrowserFallback,
  };

  return (
    <CockpitContext.Provider value={value}>{children}</CockpitContext.Provider>
  );
}

export function useCockpit(): CockpitContextValue {
  const ctx = useContext(CockpitContext);
  if (!ctx) throw new Error("useCockpit must be used within CockpitProvider");
  return ctx;
}
