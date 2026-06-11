import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../api/client.js";

function levelClass(line) {
  const t = String(line?.type || "");
  if (t.includes("dead_drop") || line?.dead_drop) return "text-danger";
  if (line?.unhealthy) return "text-warning";
  if (t === "monitor_pass" && line?.port_open && line?.pid_alive) return "text-success";
  if (t === "monitor_pass") return "text-muted";
  return "text-foreground";
}

function fmtLine(line) {
  if (!line) return "—";
  const parts = [
    line.type,
    line.epic,
    line.unhealthy != null ? (line.unhealthy ? "UNHEALTHY" : "ok") : null,
    line.consecutive_unhealthy != null ? `streak=${line.consecutive_unhealthy}` : null,
    line.port_open != null ? (line.port_open ? "port:8080↑" : "port:8080↓") : null,
    line.pid_alive != null ? (line.pid_alive ? `pid:${line.agent_pid}↑` : `pid:${line.agent_pid}↓`) : null,
  ].filter(Boolean);
  return parts.join(" · ") || JSON.stringify(line);
}

function fmtMs(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return `${Number(v).toFixed(1)}ms`;
}

/**
 * Terminal-style v27/v28 sentinel diagnostic stream with CIAO latency percentiles.
 */
export default function SentinelDiagnosticConsole() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const bottomRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await fetchJson("/api/v27/sentinel/diagnostics?limit=60");
        if (!cancelled) {
          setData(payload);
          setError("");
        }
      } catch (e) {
        if (!cancelled) setError(e.message || "diagnostics unavailable");
      }
    };
    load();
    const id = setInterval(load, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [data?.lines?.length]);

  const lines = data?.lines || [];
  const monitor = data?.monitor || {};
  const frozen = data?.safety_freeze?.operational_safety_freeze === true;
  const profiler = data?.profiler_latency || {};
  const probes = profiler.probes || {};
  const probeRows = Object.entries(probes).sort(([a], [b]) => a.localeCompare(b));

  return (
    <section className="rounded-lg border border-border bg-card p-3 sm:p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="label-caps">Sentinel diagnostics</h2>
        <div className="flex flex-wrap gap-2 text-[10px] font-mono uppercase tracking-wide">
          <span className={monitor.port_open ? "text-success" : "text-danger"}>
            :8080 {monitor.port_open ? "UP" : "DOWN"}
          </span>
          <span className={monitor.pid_alive ? "text-success" : "text-warning"}>
            PID {monitor.agent_pid ?? "—"} {monitor.pid_alive ? "ALIVE" : "STALE"}
          </span>
          {frozen && <span className="text-danger">SAFETY FREEZE</span>}
        </div>
      </div>

      {probeRows.length > 0 && (
        <div className="mb-3 overflow-x-auto rounded border border-border/60 bg-muted/20 p-2">
          <p className="mb-1.5 text-[10px] font-mono uppercase tracking-wide text-muted">
            CIAO latency · rolling {profiler.window_sec ? `${Math.round(profiler.window_sec / 60)}m` : "1h"}
          </p>
          <table className="w-full min-w-[320px] text-left font-mono text-[10px]">
            <thead>
              <tr className="text-muted">
                <th className="pb-1 pr-2 font-normal">Probe</th>
                <th className="pb-1 pr-2 font-normal">n</th>
                <th className="pb-1 pr-2 font-normal">p50</th>
                <th className="pb-1 pr-2 font-normal">p95</th>
                <th className="pb-1 font-normal">p99</th>
              </tr>
            </thead>
            <tbody>
              {probeRows.map(([name, row]) => (
                <tr key={name} className="border-t border-border/40">
                  <td className="py-0.5 pr-2 text-foreground">{name}</td>
                  <td className="py-0.5 pr-2 text-muted">{row.n ?? "—"}</td>
                  <td className="py-0.5 pr-2">{fmtMs(row.p50_ms)}</td>
                  <td className="py-0.5 pr-2 text-warning">{fmtMs(row.p95_ms)}</td>
                  <td className="py-0.5 text-danger">{fmtMs(row.p99_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="max-h-44 overflow-y-auto rounded border border-border/80 bg-[#0a0e14] p-2 font-mono text-[11px] leading-relaxed shadow-inner">
        {error && (
          <p className="text-danger">[{new Date().toLocaleTimeString()}] ERR · {error}</p>
        )}
        {lines.length === 0 && !error && (
          <p className="text-muted">[{new Date().toLocaleTimeString()}] WAIT · awaiting sentinel telemetry…</p>
        )}
        {lines.map((line, idx) => (
          <div key={line.ts || idx} className={`whitespace-pre-wrap break-all ${levelClass(line)}`}>
            <span className="text-muted/80">[{String(line.ts || "").slice(11, 19) || "—"}]</span>{" "}
            {fmtLine(line)}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <p className="mt-1.5 text-[10px] text-muted">
        v28 CIAO · poll 4s · {lines.length} events
        {probeRows.length > 0 ? ` · ${probeRows.length} probes` : ""}
      </p>
    </section>
  );
}
