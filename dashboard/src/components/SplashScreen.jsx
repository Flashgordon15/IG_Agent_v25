import { useEffect } from "react";

const V29_1_PERFORMANCE_NOTES = [
  "🚀 LOW LATENCY: Sub-microsecond Trailing Stop Evaluation Engine (~0.42µs math loop).",
  "⚡ EXECUTION: Decoupled 50ms High-Frequency Price Feed & Async Non-Blocking Broker Order Dispatch.",
  "🧠 AI PROTECTION: Dynamic Linear RSI Scaling Ramp (NaN-guarded up to 500 records).",
  "📊 ANALYTICS: Read-Only Shadow Training Registry Log Exporter (.CSV via System Tab).",
  "🛡️ RESILIENCE: Adaptive HTTP 429/Timeout Back-off Wrapper & Launchd Unattended Reboot Guardrails.",
];

/**
 * Stage 3 — release notes (full-screen during launch; drawer optional elsewhere).
 */
export default function SplashScreen({ versionData, onDismiss, variant = "launch" }) {
  const version = versionData?.version ?? "29.1.0";
  const buildDate = versionData?.build_date ?? "";
  const isLaunch = variant === "launch";

  useEffect(() => {
    const handler = (e) => {
      if (e.key === "Escape") onDismiss?.();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onDismiss]);

  const panelStyle = isLaunch
    ? {
        position: "fixed",
        top: "50%",
        left: "50%",
        transform: "translate(-50%, -50%)",
        zIndex: 50,
        width: "min(520px, calc(100vw - 2rem))",
        maxHeight: "min(85vh, 720px)",
        background: "#131929",
        border: "1px solid #1e2d45",
        borderRadius: "12px",
        overflowY: "auto",
        padding: "1.5rem",
        display: "flex",
        flexDirection: "column",
        gap: "1.25rem",
        boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
      }
    : {
        position: "fixed",
        top: 0,
        right: 0,
        bottom: 0,
        zIndex: 50,
        width: "min(480px, 100vw)",
        background: "#131929",
        borderLeft: "1px solid #1e2d45",
        overflowY: "auto",
        padding: "1.5rem",
        display: "flex",
        flexDirection: "column",
        gap: "1.25rem",
      };

  return (
    <>
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 49,
          background: "rgba(0,0,0,0.72)",
          backdropFilter: "blur(4px)",
        }}
      />
      <div style={panelStyle}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <p
              style={{
                fontSize: "10px",
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                color: "#3b82f6",
                marginBottom: "0.25rem",
              }}
            >
              IG Agent · Stage 3 of 3
            </p>
            <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e2e8f0", margin: 0 }}>
              v29.1 Performance Upgrades
            </h2>
            {buildDate && (
              <p style={{ fontSize: "11px", color: "#64748b", marginTop: "0.2rem" }}>
                Build {buildDate} · v{version}
              </p>
            )}
          </div>
        </div>

        <div
          style={{
            borderRadius: "8px",
            border: "1px solid rgba(59,130,246,0.3)",
            background: "rgba(59,130,246,0.05)",
            padding: "1rem",
          }}
        >
          <p
            style={{
              fontSize: "10px",
              textTransform: "uppercase",
              letterSpacing: "0.07em",
              color: "#3b82f6",
              fontWeight: 600,
              marginBottom: "0.75rem",
            }}
          >
            What&apos;s new
          </p>
          <ul
            style={{
              listStyle: "none",
              margin: 0,
              padding: 0,
              display: "flex",
              flexDirection: "column",
              gap: "0.65rem",
            }}
          >
            {V29_1_PERFORMANCE_NOTES.map((note) => (
              <li key={note} style={{ display: "flex", alignItems: "flex-start", gap: "0.5rem" }}>
                <span
                  style={{
                    width: "6px",
                    height: "6px",
                    borderRadius: "50%",
                    background: "#3b82f6",
                    flexShrink: 0,
                    marginTop: "5px",
                  }}
                />
                <span style={{ fontSize: "12px", color: "#94a3b8", lineHeight: 1.5 }}>{note}</span>
              </li>
            ))}
          </ul>
        </div>

        <button
          type="button"
          onClick={onDismiss}
          style={{
            width: "100%",
            borderRadius: "8px",
            background: "#3b82f6",
            color: "#fff",
            fontWeight: 600,
            fontSize: "13px",
            padding: "0.75rem",
            border: "none",
            cursor: "pointer",
            marginTop: "auto",
          }}
        >
          Launch Dashboard
        </button>
      </div>
    </>
  );
}
