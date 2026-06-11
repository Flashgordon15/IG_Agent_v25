import { useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 500;
const TIMEOUT_MS = 90_000;   // show error after 90 s with no "ready"
const MIN_DISPLAY_MS = 10_000; // always show for at least 10 seconds

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
  const [phases, setPhases]         = useState([]);
  const [pct, setPct]               = useState(0);
  const [ready, setReady]           = useState(false);
  const [minElapsed, setMinElapsed] = useState(false);
  const [error, setError]           = useState(null);
  const [fadeOut, setFadeOut]       = useState(false);
  const [displayPct, setDisplayPct] = useState(0);
  const [elapsed, setElapsed]       = useState(0);
  const timerRef = useRef(null);
  const startRef = useRef(Date.now());
  const readyRef = useRef(false);

  // Smooth display percentage counter
  useEffect(() => {
    const target = ready ? 100 : pct;
    if (displayPct === target) return;
    const id = setTimeout(
      () => setDisplayPct((v) => v + (target > v ? 1 : -1)),
      14,
    );
    return () => clearTimeout(id);
  }, [pct, displayPct, ready]);

  // Elapsed-time counter for the 10-second minimum
  useEffect(() => {
    const id = setInterval(() => {
      const ms = Date.now() - startRef.current;
      setElapsed(Math.min(ms, MIN_DISPLAY_MS));
      if (ms >= MIN_DISPLAY_MS) setMinElapsed(true);
    }, 100);
    return () => clearInterval(id);
  }, []);

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
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleEnter = () => {
    setFadeOut(true);
    setTimeout(() => onComplete?.(), 600);
  };

  const handleRetry = () => {
    setError(null);
    startRef.current = Date.now();
    setMinElapsed(false);
    readyRef.current = false;
    setReady(false);
    timerRef.current = setInterval(poll, POLL_INTERVAL_MS);
    poll();
  };

  // Show OK button once both ready AND 10s minimum have elapsed
  const showEnterButton = ready && minElapsed && !error;
  // Show a progress indicator for the minimum display time
  const minPct = Math.round((elapsed / MIN_DISPLAY_MS) * 100);

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
              v29.0
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
                onClick={handleRetry}
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

          {/* Enter Dashboard button — shown when ready + 10s elapsed */}
          {showEnterButton && (
            <button
              type="button"
              onClick={handleEnter}
              style={{
                width: "100%", borderRadius: "8px",
                background: "linear-gradient(135deg, #16a34a, #22c55e)",
                color: "#fff", fontWeight: 700, fontSize: "14px",
                padding: "0.875rem", border: "none", cursor: "pointer",
                boxShadow: "0 0 20px rgba(34,197,94,0.3)",
                letterSpacing: "0.03em",
                animation: "fadeIn 0.5s ease both",
              }}
            >
              Enter Dashboard
            </button>
          )}

          {/* Waiting indicator — systems check or minimum timer */}
          {!showEnterButton && !error && (
            <div style={{
              display: "flex", alignItems: "center", gap: "0.75rem",
              padding: "0.5rem 0",
            }}>
              {/* Minimum-time ring */}
              <svg width="32" height="32" viewBox="0 0 32 32" style={{ flexShrink: 0 }}>
                <circle cx="16" cy="16" r="13" fill="none" stroke="#1e2d45" strokeWidth="3" />
                <circle cx="16" cy="16" r="13" fill="none"
                  stroke={ready ? STATUS_COLORS.done : STATUS_COLORS.in_progress}
                  strokeWidth="3"
                  strokeDasharray={`${2 * Math.PI * 13}`}
                  strokeDashoffset={`${2 * Math.PI * 13 * (1 - minPct / 100)}`}
                  strokeLinecap="round"
                  transform="rotate(-90 16 16)"
                  style={{ transition: "stroke-dashoffset 0.1s linear" }}
                />
                <text x="16" y="20" textAnchor="middle"
                  fill={ready ? STATUS_COLORS.done : "#64748b"}
                  fontSize="9" fontWeight="600">
                  {ready ? "✓" : `${Math.round((MIN_DISPLAY_MS - elapsed) / 1000)}s`}
                </text>
              </svg>
              <div>
                <p style={{ fontSize: "12px", color: ready ? "#86efac" : "#94a3b8", margin: 0, fontWeight: ready ? 600 : 400 }}>
                  {ready ? "All systems ready — validating…" : "Running system checks…"}
                </p>
                <p style={{ fontSize: "10px", color: "#334155", margin: "0.2rem 0 0" }}>
                  {ready
                    ? `Enter button appears in ${Math.max(0, Math.round((MIN_DISPLAY_MS - elapsed) / 1000))}s`
                    : phases.find(p => p.status === "in_progress")?.label ?? "Connecting to IG…"}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
