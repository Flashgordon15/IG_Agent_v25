import { useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 500;
const TIMEOUT_MS = 90_000;       // show error after 90 s with no "ready"
const COMPLETE_LINGER_MS = 900;  // hold "100% — all systems ready" before revealing dashboard

const STATUS_COLORS = {
  done:        "#22c55e",   // green
  in_progress: "#3b82f6",   // blue
  pending:     "#334155",   // slate
  error:       "#ef4444",   // red
};

function PhaseIcon({ status }) {
  if (status === "done") {
    return (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <circle cx="8" cy="8" r="7" fill={STATUS_COLORS.done} fillOpacity="0.15"
          stroke={STATUS_COLORS.done} strokeWidth="1.5" />
        <path d="M5 8l2 2 4-4" stroke={STATUS_COLORS.done} strokeWidth="1.5"
          strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (status === "in_progress") {
    return (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none"
        style={{ animation: "spin 1s linear infinite" }}>
        <circle cx="8" cy="8" r="6" stroke={STATUS_COLORS.in_progress}
          strokeWidth="1.5" strokeOpacity="0.25" />
        <path d="M8 2 A6 6 0 0 1 14 8" stroke={STATUS_COLORS.in_progress}
          strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <circle cx="8" cy="8" r="6" stroke={STATUS_COLORS.pending}
        strokeWidth="1.5" />
    </svg>
  );
}

export default function StartupSplash({ onComplete }) {
  const [phases, setPhases]       = useState([]);
  const [pct, setPct]             = useState(0);
  const [ready, setReady]         = useState(false);
  const [error, setError]         = useState(null);
  const [fadeOut, setFadeOut]     = useState(false);
  const [displayPct, setDisplayPct] = useState(0);
  const timerRef  = useRef(null);
  const startRef  = useRef(Date.now());
  const readyRef  = useRef(false);

  // Animated display percentage — smooth CSS counter
  useEffect(() => {
    const target = ready ? 100 : pct;
    if (displayPct === target) return;
    const step = target > displayPct ? 1 : -1;
    const id = setTimeout(() => setDisplayPct((v) => v + step), 14);
    return () => clearTimeout(id);
  }, [pct, displayPct, ready]);

  const poll = async () => {
    try {
      const res = await fetch("/api/startup/status", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPhases(data.phases ?? []);
      setPct(data.overall_pct ?? 0);
      if (data.error) setError(data.error);
      if (data.ready && !readyRef.current) {
        readyRef.current = true;
        setReady(true);
        clearInterval(timerRef.current);
        // brief hold at 100% then fade out
        setTimeout(() => {
          setFadeOut(true);
          setTimeout(() => onComplete?.(), 600);
        }, COMPLETE_LINGER_MS);
      }
    } catch {
      // server not yet up — silently retry
    }
  };

  useEffect(() => {
    poll();
    timerRef.current = setInterval(() => {
      if (Date.now() - startRef.current > TIMEOUT_MS && !readyRef.current) {
        clearInterval(timerRef.current);
        setError("Agent startup timed out — check logs or restart");
      } else {
        poll();
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timerRef.current);
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const barColor = error ? STATUS_COLORS.error
    : ready ? STATUS_COLORS.done
    : STATUS_COLORS.in_progress;

  return (
    <>
      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes fadeIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
      `}</style>

      {/* Full-screen overlay */}
      <div style={{
        position: "fixed", inset: 0, zIndex: 9999,
        background: "#0b0f19",
        display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center",
        transition: "opacity 0.6s ease",
        opacity: fadeOut ? 0 : 1,
        pointerEvents: fadeOut ? "none" : "auto",
      }}>

        {/* Card */}
        <div style={{
          width: "min(480px, 92vw)",
          background: "#131929",
          border: "1px solid #1e2d45",
          borderRadius: "16px",
          padding: "2rem 2rem 1.75rem",
          display: "flex", flexDirection: "column", gap: "1.5rem",
          animation: "fadeIn 0.4s ease both",
        }}>

          {/* Branding */}
          <div style={{ textAlign: "center" }}>
            <p style={{
              fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.12em",
              color: "#3b82f6", marginBottom: "0.5rem", fontWeight: 600,
            }}>
              IG Agent
            </p>
            <h1 style={{ fontSize: "1.5rem", fontWeight: 700, color: "#e2e8f0", margin: 0 }}>
              v25
            </h1>
            <p style={{ fontSize: "11px", color: "#475569", marginTop: "0.3rem" }}>
              {ready ? "All systems ready" : error ? "Startup error" : "Starting up…"}
            </p>
          </div>

          {/* Progress bar */}
          <div>
            <div style={{
              height: "6px", background: "#1e2d45", borderRadius: "3px",
              overflow: "hidden",
            }}>
              <div style={{
                height: "100%",
                width: `${displayPct}%`,
                background: barColor,
                borderRadius: "3px",
                transition: "width 0.2s ease, background 0.4s ease",
                boxShadow: error ? "none" : `0 0 8px ${barColor}66`,
              }} />
            </div>
            <div style={{
              display: "flex", justifyContent: "space-between",
              marginTop: "0.4rem",
            }}>
              <span style={{ fontSize: "11px", color: "#475569" }}>
                {phases.find(p => p.status === "in_progress")?.label ?? (ready ? "Complete" : "Waiting…")}
              </span>
              <span style={{
                fontSize: "11px", fontWeight: 600, fontVariantNumeric: "tabular-nums",
                color: ready ? STATUS_COLORS.done : "#94a3b8",
              }}>
                {displayPct}%
              </span>
            </div>
          </div>

          {/* Phase checklist */}
          <div style={{
            display: "flex", flexDirection: "column", gap: "0.6rem",
          }}>
            {phases.map((phase) => (
              <div key={phase.id} style={{
                display: "flex", alignItems: "center", gap: "0.625rem",
                opacity: phase.status === "pending" ? 0.4 : 1,
                transition: "opacity 0.3s ease",
              }}>
                <PhaseIcon status={phase.status} />
                <span style={{
                  fontSize: "12px",
                  color: phase.status === "done" ? "#94a3b8"
                    : phase.status === "in_progress" ? "#e2e8f0"
                    : "#475569",
                  flex: 1,
                }}>
                  {phase.label}
                </span>
                {phase.note && (
                  <span style={{ fontSize: "10px", color: "#3b82f6", letterSpacing: "0.02em" }}>
                    {phase.note}
                  </span>
                )}
                {phase.status === "done" && (
                  <span style={{ fontSize: "10px", color: "#334155" }}>
                    {phase.pct}%
                  </span>
                )}
              </div>
            ))}
          </div>

          {/* Error banner */}
          {error && (
            <div style={{
              background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)",
              borderRadius: "8px", padding: "0.75rem 1rem",
              display: "flex", justifyContent: "space-between", alignItems: "center",
            }}>
              <span style={{ fontSize: "12px", color: "#fca5a5" }}>{error}</span>
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  startRef.current = Date.now();
                  timerRef.current = setInterval(poll, POLL_INTERVAL_MS);
                  poll();
                }}
                style={{
                  background: "rgba(239,68,68,0.2)", border: "1px solid rgba(239,68,68,0.4)",
                  borderRadius: "4px", color: "#fca5a5", cursor: "pointer",
                  fontSize: "11px", padding: "0.25rem 0.6rem", marginLeft: "0.75rem",
                  whiteSpace: "nowrap",
                }}
              >
                Retry
              </button>
            </div>
          )}

          {/* Footer hint */}
          {!ready && !error && (
            <p style={{
              fontSize: "10px", color: "#334155", textAlign: "center", margin: 0,
            }}>
              Authenticating with IG and loading market data…
            </p>
          )}
        </div>
      </div>
    </>
  );
}
