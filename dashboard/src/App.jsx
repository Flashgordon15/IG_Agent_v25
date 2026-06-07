import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { WS_URL } from "./config.js";
import { fetchState, fetchSplash, dismissSplash } from "./api.js";

const HEARTBEAT_INTERVAL_MS = 25000; // ping every 25 s (server times out at 10 min)
import Header from "./components/Header.jsx";
import LivePanel from "./components/LivePanel.jsx";
import TradesPanel from "./components/TradesPanel.jsx";
import PointsPanel from "./components/PointsPanel.jsx";
import IntelligencePanel from "./components/IntelligencePanel.jsx";
import SystemPanel from "./components/SystemPanel.jsx";
import SplashScreen from "./components/SplashScreen.jsx";
import StartupSplash from "./components/StartupSplash.jsx";

const TABS = [
  { id: "live", label: "LIVE" },
  { id: "trades", label: "TRADES" },
  { id: "points", label: "POINTS" },
  { id: "intelligence", label: "INTELLIGENCE" },
  { id: "system", label: "SYSTEM" },
];

const WS_BACKOFF_INITIAL_MS = 1000;
const WS_BACKOFF_MAX_MS = 30000;
const POLL_INTERVAL_MS = 5000;

function listMarketEpics(state) {
  if (!state) return [];
  if (Array.isArray(state.enabled_epics) && state.enabled_epics.length) {
    return state.enabled_epics.filter(Boolean);
  }
  const markets = state.markets;
  if (markets && typeof markets === "object") {
    return Object.keys(markets);
  }
  if (state.epic) return [state.epic];
  return [];
}

function resolveEpicPositions(state, selectedEpic, slice) {
  // Prefer the per-market slice positions (backend stores them here)
  if (Array.isArray(slice?.positions) && slice.positions.length > 0) {
    const name = slice.market_name ?? slice.market ?? selectedEpic;
    return slice.positions.map((p) => ({
      epic: selectedEpic,
      market: p.market ?? name,
      ...p,
    }));
  }
  // Fall back to top-level positions filtered by epic
  if (Array.isArray(state?.positions) && state.positions.length > 0) {
    const filtered = state.positions.filter((p) => !p.epic || p.epic === selectedEpic);
    return filtered.length > 0 ? filtered : state.positions;
  }
  return [];
}

function resolveMarketView(state, selectedEpic) {
  if (!state) return null;
  const markets = state.markets;
  if (!markets || !selectedEpic || !markets[selectedEpic]) {
    return state;
  }
  const slice = markets[selectedEpic];
  return {
    ...state,
    ...slice,
    points: state.points,
    positions: resolveEpicPositions(state, selectedEpic, slice),
    balance_gbp: state.balance_gbp,
    daily_pnl_gbp: state.daily_pnl_gbp,
    win_rate_20: state.win_rate_20,
    markets,
    selected_epic: selectedEpic,
  };
}

function positionKey(position) {
  return (
    position?.deal_id ??
    position?.id ??
    `${position?.epic ?? ""}-${position?.entry ?? position?.entry_price ?? ""}`
  );
}

function detectSoundAlerts(prev, next) {
  const alerts = [];

  if (typeof next?.sound_alert === "string") {
    alerts.push(next.sound_alert);
  }
  if (Array.isArray(next?.sound_alerts)) {
    alerts.push(...next.sound_alerts);
  }

  if (!prev || !next) {
    return alerts.filter((a) => a !== "stop_state");
  }

  const prevPositions = prev.positions ?? [];
  const nextPositions = next.positions ?? [];
  const prevIds = new Set(prevPositions.map(positionKey));
  const nextIds = new Set(nextPositions.map(positionKey));

  for (const position of nextPositions) {
    if (!prevIds.has(positionKey(position))) {
      alerts.push("trade_open");
    }
  }

  for (const position of prevPositions) {
    if (!nextIds.has(positionKey(position))) {
      const pnl =
        position?.unrealised_pnl_gbp ??
        position?.pnl_gbp ??
        position?.unrealised_pnl ??
        0;
      alerts.push(Number(pnl) >= 0 ? "trade_win" : "trade_loss");
    }
  }

  return alerts.filter((a) => a !== "stop_state");
}

function createSoundEngine() {
  let ctx = null;
  let stopAlarmId = null;

  const ensureContext = () => {
    if (!ctx) {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) ctx = new AudioCtx();
    }
    if (ctx?.state === "suspended") {
      ctx.resume().catch(() => {});
    }
    return ctx;
  };

  const playTone = (frequency, durationMs, delayMs = 0) => {
    const audioCtx = ensureContext();
    if (!audioCtx) return;

    const start = audioCtx.currentTime + delayMs / 1000;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();

    osc.type = "sine";
    osc.frequency.value = frequency;
    gain.gain.setValueAtTime(0.12, start);
    gain.gain.exponentialRampToValueAtTime(0.001, start + durationMs / 1000);

    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(start);
    osc.stop(start + durationMs / 1000 + 0.02);
  };

  return {
    ensureContext,
    playAlert(name) {
      switch (name) {
        case "trade_open":
          playTone(440, 200);
          break;
        case "trade_win":
          playTone(440, 150, 0);
          playTone(660, 200, 170);
          break;
        case "trade_loss":
          playTone(220, 300);
          break;
        case "stop_state":
          this.startStopAlarm();
          break;
        default:
          break;
      }
    },
    startStopAlarm() {
      if (stopAlarmId) return;
      const beep = () => playTone(880, 200);
      beep();
      stopAlarmId = window.setInterval(beep, 650);
    },
    stopStopAlarm() {
      if (stopAlarmId) {
        window.clearInterval(stopAlarmId);
        stopAlarmId = null;
      }
    },
  };
}

export default function App() {
  const [tab, setTab] = useState("live");
  const [state, setState] = useState(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(true);
  const [selectedEpic, setSelectedEpic] = useState(null);
  const [splashData, setSplashData] = useState(null);
  const [splashVisible, setSplashVisible] = useState(false);
  // startupDone: null = checking, false = show splash, true = skip splash
  const [startupDone, setStartupDone] = useState(null);
  // "idle" | "confirming" | "stopping" | "verifying" | "stopped"
  const [shutdownState, setShutdownState] = useState("idle");
  const [shutdownVerification, setShutdownVerification] = useState(null);
  const [agentStillRunning, setAgentStillRunning] = useState(false);
  const [agentAlive, setAgentAlive] = useState(true);
  const healthFailRef = useRef(0);
  const prevStateRef = useRef(null);
  const soundRef = useRef(null);

  if (!soundRef.current) {
    soundRef.current = createSoundEngine();
  }

  const applyState = useCallback((next) => {
    if (next && typeof next === "object") {
      setState(next);
    }
  }, []);

  // Always show the startup splash on every fresh page load.
  // If the agent is already running (ready:true on first poll), the splash
  // ticks all phases instantly and auto-dismisses after a brief hold.
  // This ensures the user always sees the splash when clicking the desktop icon.
  useEffect(() => {
    setStartupDone(false);
  }, []);

  // Version changelog splash: once per version (localStorage), AFTER startup
  useEffect(() => {
    if (!startupDone) return;
    fetchSplash().then((data) => {
      if (!data) return;
      const version = String(data.version ?? "unknown");
      const seenKey = `ig_agent_splash_seen_${version}`;
      if (typeof window !== "undefined" && window.localStorage.getItem(seenKey)) {
        return;
      }
      setSplashData(data);
      setSplashVisible(true);
    });
  }, [startupDone]);

  const handleSplashDismiss = useCallback(() => {
    setSplashVisible(false);
    const version = splashData?.version;
    if (version && typeof window !== "undefined") {
      window.localStorage.setItem(`ig_agent_splash_seen_${version}`, "1");
    }
    dismissSplash();
  }, [splashData]);

  useEffect(() => {
    const epics = listMarketEpics(state);
    if (!epics.length) return;
    const preferred = state?.selected_epic || epics[0];
    if (!selectedEpic || !epics.includes(selectedEpic)) {
      setSelectedEpic(preferred);
    }
  }, [state, selectedEpic]);

  const viewState = useMemo(
    () => resolveMarketView(state, selectedEpic),
    [state, selectedEpic],
  );

  useEffect(() => {
    let mounted = true;
    let ws = null;
    let reconnectTimer = null;
    let pollTimer = null;
    let backoffMs = WS_BACKOFF_INITIAL_MS;

    const poll = async () => {
      const data = await fetchState();
      if (mounted) applyState(data);
    };

    const startPolling = () => {
      if (pollTimer) return;
      poll();
      pollTimer = window.setInterval(poll, POLL_INTERVAL_MS);
    };

    const stopPolling = () => {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const connect = () => {
      if (!mounted) return;

      setWsConnected(false);
      setReconnecting(true);
      startPolling();

      ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        if (!mounted) return;
        setWsConnected(true);
        setReconnecting(false);
        backoffMs = WS_BACKOFF_INITIAL_MS;
        stopPolling();
      };

      ws.onmessage = (event) => {
        try {
          applyState(JSON.parse(event.data));
        } catch {
          /* ignore malformed frames */
        }
      };

      ws.onclose = () => {
        if (!mounted) return;
        setWsConnected(false);
        setReconnecting(true);
        startPolling();
        reconnectTimer = window.setTimeout(connect, backoffMs);
        backoffMs = Math.min(WS_BACKOFF_MAX_MS, backoffMs * 2);
      };

      ws.onerror = () => ws.close();
    };

    connect();

    return () => {
      mounted = false;
      stopPolling();
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [applyState]);

  useEffect(() => {
    const resumeAudio = () => soundRef.current?.ensureContext();
    window.addEventListener("pointerdown", resumeAudio, { once: true });
    return () => window.removeEventListener("pointerdown", resumeAudio);
  }, []);

  useEffect(() => {
    if (!state) return;

    const prev = prevStateRef.current;
    if (prev) {
      for (const alert of detectSoundAlerts(prev, state)) {
        soundRef.current?.playAlert(alert);
      }
    }
    prevStateRef.current = state;
  }, [state]);

  const inStopState =
    state?.points?.state === "STOP" || state?.trading_paused === true;

  useEffect(() => {
    if (shutdownState !== "idle") {
      soundRef.current?.stopStopAlarm();
      return;
    }
    if (inStopState) {
      soundRef.current?.startStopAlarm();
    } else {
      soundRef.current?.stopStopAlarm();
    }
  }, [inStopState, shutdownState]);

  useEffect(() => {
    return () => soundRef.current?.stopStopAlarm();
  }, []);

  // Heartbeat — dashboard liveness ping (auto-shutdown on disconnect is disabled)
  useEffect(() => {
    if (startupDone !== true) return;
    const ping = () => fetch("/api/heartbeat", { method: "POST" }).catch(() => {});
    ping(); // immediate first ping
    const id = window.setInterval(ping, HEARTBEAT_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [startupDone]);

  // Agent liveness — distinguish dead backend from closed markets / CAUTION
  useEffect(() => {
    if (startupDone !== true) return;
    const checkHealth = async () => {
      try {
        const res = await fetch("/api/health", { method: "GET" });
        if (res.ok) {
          healthFailRef.current = 0;
          setAgentAlive(true);
          return;
        }
      } catch {
        /* unreachable */
      }
      healthFailRef.current += 1;
      if (healthFailRef.current >= 3) {
        setAgentAlive(false);
      }
    };
    checkHealth();
    const id = window.setInterval(checkHealth, 10000);
    return () => window.clearInterval(id);
  }, [startupDone]);

  const handleStopAgent = useCallback(() => {
    setShutdownState("confirming");
  }, []);

  const verifyRowsFromResult = useCallback((result) => {
    if (!result) return [];
    if (Array.isArray(result.checks) && result.checks.length > 0) {
      return result.checks;
    }
    return (result.issues || []).map((issue) => ({
      label: issue,
      ok: false,
      detail: "",
    }));
  }, []);

  const pollShutdownVerify = useCallback(async (urls, timeoutMs = 45000) => {
    const pollUrls = (Array.isArray(urls) ? urls : [urls]).filter(Boolean);
    const deadline = Date.now() + timeoutMs;
    let lastPayload = null;
    while (Date.now() < deadline) {
      for (const url of pollUrls) {
        try {
          const res = await fetch(url);
          if (res.ok) {
            const data = await res.json();
            lastPayload = data;
            if (data.status === "done") {
              return data;
            }
          }
        } catch {
          // try next verify source
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    if (lastPayload) {
      return lastPayload;
    }
    return {
      ok: false,
      checks: [],
      issues: ["verification timed out — run: PYTHONPATH=src python3 scripts/confirm_stopped.py"],
    };
  }, []);

  const handleStopConfirm = useCallback(async () => {
    setShutdownState("stopping");
    setShutdownVerification(null);
    soundRef.current?.stopStopAlarm();
    let cleanupChecks = [];
    const verifyUrls = [
      "http://127.0.0.1:8081/shutdown-verify",
      "/api/shutdown/verify-status",
    ];
    const controller = new AbortController();
    const abortTimer = window.setTimeout(() => controller.abort(), 8000);
    try {
      const res = await fetch("/api/shutdown", {
        method: "POST",
        signal: controller.signal,
      });
      if (res.ok) {
        const data = await res.json();
        cleanupChecks = data.cleanup_checks || [];
        if (data.verify_poll_url) {
          verifyUrls[0] = data.verify_poll_url;
        }
        if (data.verify_fallback_url) {
          verifyUrls[1] = data.verify_fallback_url;
        }
      }
    } catch {
      // connection may drop as the process exits — continue to verification
    } finally {
      window.clearTimeout(abortTimer);
    }
    setShutdownState("verifying");
    const finalVerify = await pollShutdownVerify(verifyUrls);
    setShutdownVerification({
      cleanup: cleanupChecks,
      final: finalVerify,
      ok: Boolean(finalVerify?.ok),
    });
    setShutdownState("stopped");
  }, [pollShutdownVerify]);

  const handleStopCancel = useCallback(() => {
    setShutdownState("idle");
  }, []);

  useEffect(() => {
    if (shutdownState !== "stopped") {
      setAgentStillRunning(false);
      return undefined;
    }
    let cancelled = false;
    const probe = async () => {
      try {
        const res = await fetch("/api/health", { signal: AbortSignal.timeout(2000) });
        if (!cancelled && res.ok) {
          setAgentStillRunning(true);
        }
      } catch {
        if (!cancelled) {
          setAgentStillRunning(false);
        }
      }
    };
    probe();
    const timer = window.setInterval(probe, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [shutdownState]);

  const headerProps = {
    state: viewState,
    bid: viewState?.bid,
    offer: viewState?.offer,
    agentState: state?.points?.state ?? state?.agent_state,
    tradingLoopsRunning: state?.trading_loops_running,
    tradingPaused: state?.trading_paused,
    tradingHealthy: state?.trading_healthy,
    agentAlive,
    pointsTrade: state?.points?.last_trade,
    pointsSession: state?.points?.session,
    pointsCumulative: state?.points?.cumulative,
    fitness: viewState?.signal?.fitness,
    winRate: state?.win_rate_20,
    dailyPnl: state?.daily_pnl_gbp,
    streamStatus: viewState?.stream_status,
    marketState: viewState?.market_state,
    epic: viewState?.epic ?? selectedEpic,
    spreadCurrent: viewState?.spread_current ?? viewState?.spread,
    spreadNormal: viewState?.spread_normal,
    sentiment: viewState?.sentiment,
    wsConnected,
    reconnecting,
    openPositions: (state?.positions ?? []).length,
    maxPositions: state?.max_open_positions ?? 10,
    onStopAgent: handleStopAgent,
  };

  // Agent stopped screen (with post-shutdown verification)
  if (shutdownState === "stopped") {
    const agentDown = !agentStillRunning;
    const fullyVerified = shutdownVerification?.ok === true && agentDown;
    const agentStoppedVerify = agentDown
      && (shutdownVerification?.final?.checks || []).some(
        (row) => row.label === "No main.py process" && row.ok,
      );
    const checkRows = [
      ...(shutdownVerification?.cleanup || []),
      ...verifyRowsFromResult(shutdownVerification?.final),
    ];
    return (
      <div style={{
        position: "fixed", inset: 0, background: "#0b0f19",
        display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
        gap: "12px", padding: "24px",
      }}>
        <div style={{
          width: "40px", height: "40px", borderRadius: "50%",
          background: fullyVerified ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)",
          border: fullyVerified ? "1px solid rgba(34,197,94,0.4)" : "1px solid rgba(239,68,68,0.4)",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <span style={{
            width: "12px", height: "12px", borderRadius: "50%",
            background: fullyVerified ? "#22c55e" : "#ef4444", display: "block",
          }} />
        </div>
        <p style={{ color: "#f8fafc", fontSize: "16px", fontWeight: 600, margin: 0 }}>
          {agentStillRunning
            ? "Agent still running"
            : fullyVerified
              ? "Fully stopped — verified"
              : agentStoppedVerify
                ? "Agent stopped — safe to close tab"
                : "Stop requested"}
        </p>
        <p style={{ color: "#64748b", fontSize: "12px", margin: 0, textAlign: "center", maxWidth: "360px" }}>
          {agentStillRunning
            ? "This tab shows a stale stop screen — the agent is still alive on port 8080. Click Stop Agent again or close this tab and use the desktop icon to stop properly."
            : fullyVerified
              ? "All shutdown checks passed. Safe to close this tab or restart from the desktop icon."
              : agentStoppedVerify
                ? "Trading agent is down. You may close this browser tab. If any watchdog check failed, it will not restart the agent while manual stop is active."
                : "Verification did not fully pass. See checks below or run confirm_stopped.py in Terminal."}
        </p>
        {checkRows.length > 0 && (
          <ul style={{
            listStyle: "none", margin: "8px 0 0", padding: 0, width: "min(360px, 92vw)",
            maxHeight: "40vh", overflowY: "auto", textAlign: "left", fontSize: "11px",
          }}>
            {checkRows.map((row, idx) => (
              <li
                key={`${row.label}-${row.detail}-${idx}`}
                style={{ color: row.ok ? "#22c55e" : "#ef4444", marginBottom: "4px" }}
              >
                [{row.ok ? "PASS" : "FAIL"}] {row.label}
                {row.detail ? ` — ${row.detail}` : ""}
              </li>
            ))}
          </ul>
        )}
        {agentStillRunning && (
          <button
            type="button"
            onClick={() => {
              setShutdownState("idle");
              setShutdownVerification(null);
              setAgentStillRunning(false);
            }}
            style={{
              marginTop: "8px",
              padding: "8px 16px",
              borderRadius: "6px",
              border: "1px solid #374151",
              background: "transparent",
              color: "#94a3b8",
              fontSize: "12px",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Return to dashboard — stop again
          </button>
        )}
      </div>
    );
  }

  // Show startup splash while agent is initialising (null = still checking initial status)
  if (startupDone === false) {
    return <StartupSplash onComplete={() => setStartupDone(true)} />;
  }

  // Thin loading state while the initial /api/startup/status fetch completes
  if (startupDone === null) {
    return (
      <div style={{
        position: "fixed", inset: 0, background: "#0b0f19",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        <p style={{ fontSize: "12px", color: "#334155" }}>Connecting…</p>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen min-w-0 flex-col bg-bg text-foreground">
      <Header {...headerProps} />

      {/* Stop confirmation modal */}
      {(shutdownState === "confirming" || shutdownState === "stopping" || shutdownState === "verifying") && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 9999,
          background: "rgba(0,0,0,0.7)", backdropFilter: "blur(4px)",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <div style={{
            background: "#111827", border: "1px solid #374151",
            borderRadius: "12px", padding: "28px 32px", maxWidth: "360px", width: "90%",
            textAlign: "center",
          }}>
            {shutdownState === "stopping" || shutdownState === "verifying" ? (
              <>
                <p style={{ color: "#f8fafc", fontSize: "15px", fontWeight: 600, marginBottom: "8px" }}>
                  {shutdownState === "verifying" ? "Verifying shutdown…" : "Shutting down…"}
                </p>
                <p style={{ color: "#64748b", fontSize: "12px" }}>
                  {shutdownState === "verifying"
                    ? "Confirming no agent, watchdog, lock, or port 8080 remains."
                    : "Saving session state and stopping trading loops."}
                </p>
              </>
            ) : (
              <>
                <div style={{
                  width: "44px", height: "44px", borderRadius: "50%",
                  background: "rgba(239,68,68,0.12)", border: "1px solid rgba(239,68,68,0.35)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  margin: "0 auto 16px",
                }}>
                  <span style={{ fontSize: "18px" }}>⏹</span>
                </div>
                <p style={{ color: "#f8fafc", fontSize: "15px", fontWeight: 600, marginBottom: "6px" }}>
                  Stop Agent?
                </p>
                <p style={{ color: "#64748b", fontSize: "12px", marginBottom: "24px", lineHeight: 1.5 }}>
                  This will stop all trading loops, save the session log, and shut down the agent process.
                  Any open positions will remain open on IG.
                </p>
                <div style={{ display: "flex", gap: "10px", justifyContent: "center" }}>
                  <button
                    onClick={handleStopCancel}
                    style={{
                      padding: "8px 20px", borderRadius: "6px",
                      border: "1px solid #374151", background: "transparent",
                      color: "#94a3b8", fontSize: "12px", fontWeight: 600, cursor: "pointer",
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleStopConfirm}
                    style={{
                      padding: "8px 20px", borderRadius: "6px",
                      border: "1px solid rgba(239,68,68,0.5)", background: "rgba(239,68,68,0.15)",
                      color: "#ef4444", fontSize: "12px", fontWeight: 600, cursor: "pointer",
                    }}
                  >
                    Stop Agent
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
      {splashVisible && (
        <SplashScreen versionData={splashData} onDismiss={handleSplashDismiss} />
      )}

      <nav className="sticky top-0 z-10 flex shrink-0 gap-0 overflow-x-auto border-b border-border bg-card px-1 sm:px-2">
        {TABS.map((item) => {
          const active = tab === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setTab(item.id)}
              className={[
                "shrink-0 border-b-2 px-2 py-2.5 text-[10px] font-semibold uppercase tracking-wide transition-colors sm:px-4 sm:text-xs",
                active
                  ? "border-accent text-foreground"
                  : "border-transparent text-muted hover:text-foreground",
              ].join(" ")}
            >
              {item.label}
            </button>
          );
        })}
      </nav>

      {!wsConnected && (
        <div className="bg-warning/15 px-3 py-1.5 text-center text-[11px] text-warning sm:text-xs">
          {reconnecting
            ? "WebSocket disconnected — polling /api/state every 5s"
            : "Connecting…"}
        </div>
      )}

      <main className="min-h-0 flex-1 overflow-y-auto px-2 py-3 sm:px-4 sm:py-4">
        {tab === "live" && (
          <LivePanel
            state={viewState}
            rawState={state}
            selectedEpic={selectedEpic}
            onSelectEpic={setSelectedEpic}
            wsConnected={wsConnected}
          />
        )}
        {tab === "trades" && <TradesPanel state={state} />}
        {tab === "points" && <PointsPanel state={state} />}
        {tab === "intelligence" && <IntelligencePanel state={state} />}
        {tab === "system" && (
          <SystemPanel
            state={state}
            wsConnected={wsConnected}
            reconnecting={reconnecting}
          />
        )}
      </main>
    </div>
  );
}
