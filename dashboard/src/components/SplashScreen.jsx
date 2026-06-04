import { useEffect } from "react";

export default function SplashScreen({ versionData, onDismiss }) {
  const version = versionData?.version ?? "25.x";
  const buildDate = versionData?.build_date ?? "";
  const changelog = Array.isArray(versionData?.changelog) ? versionData.changelog : [];
  const latest = changelog[0] ?? null;
  const older = changelog.slice(1, 3);

  // Close on Escape key
  useEffect(() => {
    const handler = (e) => { if (e.key === "Escape") onDismiss?.(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onDismiss]);

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onDismiss}
        style={{
          position: "fixed", inset: 0, zIndex: 49,
          background: "rgba(0,0,0,0.6)", backdropFilter: "blur(2px)",
        }}
      />
      {/* Panel — right-side drawer */}
      <div style={{
        position: "fixed", top: 0, right: 0, bottom: 0, zIndex: 50,
        width: "min(480px, 100vw)",
        background: "#131929",
        borderLeft: "1px solid #1e2d45",
        overflowY: "auto",
        padding: "1.5rem",
        display: "flex",
        flexDirection: "column",
        gap: "1.25rem",
      }}>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <p style={{ fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.08em", color: "#3b82f6", marginBottom: "0.25rem" }}>
              IG Agent
            </p>
            <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e2e8f0", margin: 0 }}>
              What&apos;s New · v{version}
            </h2>
            {buildDate && (
              <p style={{ fontSize: "11px", color: "#64748b", marginTop: "0.2rem" }}>Build {buildDate}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onDismiss}
            style={{
              background: "none", border: "1px solid #1e2d45", borderRadius: "6px",
              color: "#64748b", cursor: "pointer", padding: "0.25rem 0.6rem",
              fontSize: "12px",
            }}
          >
            Close
          </button>
        </div>

        {/* Latest release */}
        {latest && (
          <div style={{
            borderRadius: "8px", border: "1px solid rgba(59,130,246,0.3)",
            background: "rgba(59,130,246,0.05)", padding: "1rem",
          }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.6rem" }}>
              <span style={{ fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.07em", color: "#3b82f6", fontWeight: 600 }}>
                v{latest.version}
              </span>
              <span style={{ fontSize: "11px", color: "#64748b" }}>{latest.date}</span>
            </div>
            <p style={{ fontWeight: 500, color: "#e2e8f0", marginBottom: "0.75rem", fontSize: "13px" }}>
              {latest.title}
            </p>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              {(latest.changes ?? []).map((c, i) => {
                const isFixed = String(c).startsWith("Fixed:");
                const isNew = String(c).startsWith("New:");
                const isChanged = String(c).startsWith("Changed:");
                const dotColor = isFixed ? "#22c55e" : isNew ? "#3b82f6" : isChanged ? "#f59e0b" : "#64748b";
                return (
                  <li key={i} style={{ display: "flex", alignItems: "flex-start", gap: "0.5rem" }}>
                    <span style={{
                      width: "6px", height: "6px", borderRadius: "50%",
                      background: dotColor, flexShrink: 0, marginTop: "5px",
                    }} />
                    <span style={{ fontSize: "12px", color: "#94a3b8", lineHeight: 1.5 }}>{c}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {/* Prior releases */}
        {older.length > 0 && (
          <div style={{ borderRadius: "8px", border: "1px solid #1e2d45", background: "#0b0f19", padding: "0.875rem" }}>
            <p style={{ fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.07em", color: "#64748b", marginBottom: "0.75rem" }}>
              Previous releases
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
              {older.map((rel) => (
                <div key={rel.version} style={{ display: "flex", alignItems: "baseline", gap: "0.75rem" }}>
                  <span style={{ fontSize: "12px", fontWeight: 600, color: "#e2e8f0" }}>v{rel.version}</span>
                  <span style={{ fontSize: "11px", color: "#64748b" }}>{rel.date} · {rel.title}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Dismiss button */}
        <button
          type="button"
          onClick={onDismiss}
          style={{
            width: "100%", borderRadius: "8px", background: "#3b82f6",
            color: "#fff", fontWeight: 600, fontSize: "13px",
            padding: "0.75rem", border: "none", cursor: "pointer",
            marginTop: "auto",
          }}
        >
          Got it — v{version}
        </button>
      </div>
    </>
  );
}
