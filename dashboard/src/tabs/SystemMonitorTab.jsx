import { useCallback, useEffect, useMemo, useRef, useState, memo } from "react";
import { authHeaders } from "../api/client.js";
import { API_BASE, resolveTargetPort } from "../config.js";
import { useApexTelemetry } from "../apex/useApexTelemetry.js";
import { useSidecarPid } from "../hooks/useSidecarPid.js";

const EMPTY_MSG = "Awaiting initial market telemetry streams…";
const POLL_MS = 2000;
const EXPORT_FLASH_MS = 5000;

function agentApiUrl(path = "") {
  const port = resolveTargetPort();
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `http://127.0.0.1:${port}${suffix}`;
}

async function fetchSystemMonitor() {
  const res = await fetch(agentApiUrl("/api/apex/system-monitor"), {
    credentials: "include",
    headers: authHeaders(),
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok) throw new Error(`monitor ${res.status}`);
  return res.json();
}

async function requestWarmupExport() {
  if (typeof window !== "undefined" && window.apexIPC?.exportWarmupReport) {
    return window.apexIPC.exportWarmupReport();
  }
  const res = await fetch(agentApiUrl("/api/apex/export-warmup-report"), {
    method: "POST",
    credentials: "include",
    headers: authHeaders(),
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`export ${res.status}`);
  return res.json();
}

/**
 * Native standalone System Monitor — glass terminal + WAL report export.
 */
function SystemMonitorTab() {
  const { telemetry } = useApexTelemetry();
  const sidecarPid = useSidecarPid(telemetry?.tick?.agent_pid);
  const [monitor, setMonitor] = useState(null);
  const [booting, setBooting] = useState(true);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportFlash, setExportFlash] = useState(false);
  const [exportPath, setExportPath] = useState("");
  const terminalRef = useRef(null);

  const poll = useCallback(async () => {
    try {
      const data = await fetchSystemMonitor();
      if (data && typeof data === "object") {
        setMonitor(data);
        setBooting(false);
      }
    } catch {
      /* sidecar warming */
    }
  }, []);

  useEffect(() => {
    poll();
    const id = window.setInterval(poll, POLL_MS);
    return () => window.clearInterval(id);
  }, [poll]);

  const funnel = useMemo(() => {
    const fromMonitor = monitor?.funnel;
    const fromTick = telemetry?.transparency?.funnel;
    if (fromMonitor && typeof fromMonitor === "object") return fromMonitor;
    if (fromTick && typeof fromTick === "object") return fromTick;
    return null;
  }, [monitor, telemetry]);

  const lines = useMemo(() => {
    const raw = monitor?.lines;
    if (!Array.isArray(raw)) return [];
    return raw.map((row) => String(row?.line ?? "")).filter(Boolean);
  }, [monitor]);

  const pid = sidecarPid ?? monitor?.pid ?? telemetry?.tick?.agent_pid ?? "—";
  const bridgeSec = monitor?.bridge_sec_ago ?? telemetry?.transparency?.health_grid?.bridge_connected_sec_ago;

  useEffect(() => {
    const el = terminalRef.current;
    if (el) el.scrollTop = 0;
  }, [lines.length]);

  const handleExport = async () => {
    setExportBusy(true);
    try {
      const result = await requestWarmupExport();
      if (result?.ok) {
        setExportPath(String(result.path || "logs/warmup_report_latest.md"));
        setExportFlash(true);
        window.setTimeout(() => setExportFlash(false), EXPORT_FLASH_MS);
        await poll();
      }
    } catch {
      /* export failed — terminal will show next poll */
    } finally {
      setExportBusy(false);
    }
  };

  const hasTelemetry = lines.length > 0 || funnel != null;

  return (
    <div className="apex-sysmon">
      <header className="apex-sysmon__header">
        <div>
          <h1 className="apex-sysmon__title">📟 System Monitor</h1>
          <p className="apex-sysmon__sub">
            Native operational telemetry — PID {pid}
            {bridgeSec != null ? ` · bridge ${Number(bridgeSec).toFixed(1)}s ago` : ""}
          </p>
        </div>
        <div className="apex-sysmon__export-wrap">
          <button
            type="button"
            className="apex-sysmon__export-btn"
            disabled={exportBusy}
            onClick={handleExport}
          >
            {exportBusy ? "Exporting…" : "EXPORT PERFORMANCE TRACK RECORD"}
          </button>
          {exportFlash && (
            <span className="apex-sysmon__export-ok" role="status">
              REPORT EXPORTED SUCCESSFULLY
            </span>
          )}
          {exportPath && exportFlash && (
            <span className="apex-sysmon__export-path">{exportPath}</span>
          )}
        </div>
      </header>

      {funnel && (
        <div className="apex-sysmon__counters">
          <span>Scanned: {(funnel?.opportunities_scanned ?? 0).toLocaleString()}</span>
          <span>Spread ↯: {(funnel?.spread_rejections ?? 0).toLocaleString()}</span>
          <span>Liquidity ↯: {(funnel?.liquidity_blocks ?? 0).toLocaleString()}</span>
          <span>ML Veto: {(funnel?.ml_veto_flags ?? 0).toLocaleString()}</span>
          <span>Executed: {(funnel?.executed_trades ?? 0).toLocaleString()}</span>
        </div>
      )}

      <div className="apex-sysmon__terminal" ref={terminalRef} role="log" aria-live="polite">
        {!hasTelemetry && booting ? (
          <p className="apex-sysmon__line apex-sysmon__line--placeholder">
            <span className="apex-sysmon__pulse" aria-hidden="true" />
            {EMPTY_MSG}
          </p>
        ) : lines.length === 0 ? (
          <p className="apex-sysmon__line apex-sysmon__line--placeholder">{EMPTY_MSG}</p>
        ) : (
          lines.map((line, i) => (
            <p key={`${line}-${i}`} className="apex-sysmon__line">
              {line}
            </p>
          ))
        )}
      </div>
    </div>
  );
}

export default memo(SystemMonitorTab);
