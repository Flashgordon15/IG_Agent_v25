import { useEffect, useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import { HELP_SECTIONS, STRATEGY_HELP_VERSION } from "../content/strategyHelp.js";

const OVERLAY_STYLE = {
  position: "fixed",
  inset: 0,
  zIndex: 10000,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: "1.5rem",
  background: "rgba(0, 0, 0, 0.82)",
  backdropFilter: "blur(4px)",
};

const PANEL_STYLE = {
  width: "min(48rem, 100%)",
  maxHeight: "min(90vh, 840px)",
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
  borderRadius: "12px",
  border: "1px solid #475569",
  background: "#1a2236",
  boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.75)",
};

function HelpTable({ table }) {
  if (!table) return null;
  return (
    <div
      className="mt-3 overflow-x-auto rounded-lg"
      style={{ border: "1px solid #475569" }}
    >
      <table className="w-full min-w-[480px] text-left text-[11px]">
        <thead>
          <tr style={{ borderBottom: "1px solid #475569", background: "rgba(30, 41, 59, 0.7)" }}>
            {table.headers.map((h) => (
              <th key={h} className="px-2.5 py-2 font-semibold" style={{ color: "#f1f5f9" }}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, i) => (
            <tr key={i} style={{ borderBottom: "1px solid rgba(71, 85, 105, 0.5)" }}>
              {row.map((cell, j) => (
                <td
                  key={j}
                  className="px-2.5 py-2 tabular-nums"
                  style={{
                    color: j === 0 ? "#ffffff" : "#e2e8f0",
                    fontWeight: j === 0 ? 600 : 400,
                  }}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function HelpSection({ section }) {
  return (
    <section
      id={`help-${section.id}`}
      className="scroll-mt-3 pb-5"
      style={{ borderBottom: "1px solid rgba(71, 85, 105, 0.4)" }}
    >
      <h3 className="text-[13px] font-semibold" style={{ color: "#ffffff" }}>
        {section.title}
      </h3>
      {section.body && (
        <p className="mt-2 text-[12px] leading-relaxed" style={{ color: "#e2e8f0" }}>
          {section.body}
        </p>
      )}
      {section.table && <HelpTable table={section.table} />}
      {section.bullets?.length > 0 && (
        <ul
          className="mt-3 list-disc space-y-1.5 pl-4 text-[12px] leading-relaxed"
          style={{ color: "#e2e8f0" }}
        >
          {section.bullets.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function StrategyHelpModal({ open, onClose }) {
  const scrollRef = useRef(null);

  const handleKey = useCallback(
    (e) => {
      if (e.key === "Escape") onClose();
    },
    [onClose],
  );

  useEffect(() => {
    if (!open) return undefined;
    document.addEventListener("keydown", handleKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.body.style.overflow = prev;
    };
  }, [open, handleKey]);

  if (!open) return null;

  return createPortal(
    <div
      style={OVERLAY_STYLE}
      role="dialog"
      aria-modal="true"
      aria-labelledby="strategy-help-title"
      onClick={onClose}
    >
      <div style={PANEL_STYLE} onClick={(e) => e.stopPropagation()}>
        <header
          className="flex shrink-0 items-start justify-between gap-3 px-5 py-4"
          style={{ borderBottom: "1px solid #475569", background: "#131929" }}
        >
          <div className="min-w-0">
            <h2 id="strategy-help-title" className="text-[16px] font-semibold" style={{ color: "#ffffff" }}>
              Strategy &amp; logic guide
            </h2>
            <p className="mt-1 text-[11px]" style={{ color: "#cbd5e1" }}>
              How v{STRATEGY_HELP_VERSION} actually trades — gates, sizing, points &amp; ML
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-md px-3 py-1.5 text-[11px] font-semibold"
            style={{
              border: "1px solid #64748b",
              background: "#334155",
              color: "#f8fafc",
            }}
            aria-label="Close help"
          >
            Close
          </button>
        </header>

        <nav
          className="shrink-0 px-5 py-2"
          style={{ borderBottom: "1px solid #475569", background: "rgba(19, 25, 41, 0.9)" }}
        >
          <div className="flex flex-wrap gap-1.5">
            {HELP_SECTIONS.map((s) => (
              <a
                key={s.id}
                href={`#help-${s.id}`}
                className="rounded px-2 py-0.5 text-[10px] font-medium"
                style={{
                  border: "1px solid #475569",
                  background: "rgba(51, 65, 85, 0.8)",
                  color: "#e2e8f0",
                }}
              >
                {s.title.replace(/ \(config\)/, "")}
              </a>
            ))}
          </div>
        </nav>

        <div
          ref={scrollRef}
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4"
        >
          <div className="space-y-5">
            {HELP_SECTIONS.map((section) => (
              <HelpSection key={section.id} section={section} />
            ))}
          </div>
          <p className="mt-6 pb-1 text-center text-[10px]" style={{ color: "#94a3b8" }}>
            Source: config/config_v25.json + trading_loop.py — update strategyHelp.js when logic changes
          </p>
        </div>
      </div>
    </div>,
    document.body,
  );
}
