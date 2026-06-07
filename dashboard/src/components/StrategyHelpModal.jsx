import { useEffect, useCallback } from "react";
import { HELP_SECTIONS, STRATEGY_HELP_VERSION } from "../content/strategyHelp.js";

function HelpTable({ table }) {
  if (!table) return null;
  return (
    <div className="mt-3 overflow-x-auto rounded-lg border border-border">
      <table className="w-full min-w-[480px] text-left text-[11px]">
        <thead>
          <tr className="border-b border-border bg-card/80">
            {table.headers.map((h) => (
              <th key={h} className="px-2.5 py-2 font-semibold text-muted">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, i) => (
            <tr key={i} className="border-b border-border/60 last:border-0">
              {row.map((cell, j) => (
                <td
                  key={j}
                  className={`px-2.5 py-2 tabular-nums ${j === 0 ? "font-medium text-foreground" : "text-foreground/90"}`}
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
    <section id={`help-${section.id}`} className="scroll-mt-4 border-b border-border/50 pb-5 last:border-0">
      <h3 className="text-[13px] font-semibold text-foreground">{section.title}</h3>
      {section.body && (
        <p className="mt-2 text-[12px] leading-relaxed text-muted">{section.body}</p>
      )}
      {section.table && <HelpTable table={section.table} />}
      {section.bullets?.length > 0 && (
        <ul className="mt-3 list-disc space-y-1.5 pl-4 text-[12px] leading-relaxed text-foreground/90">
          {section.bullets.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function StrategyHelpModal({ open, onClose }) {
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
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.body.style.overflow = prev;
    };
  }, [open, handleKey]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[9998] flex items-center justify-center bg-black/70 p-3 backdrop-blur-sm sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="strategy-help-title"
      onClick={onClose}
    >
      <div
        className="flex max-h-[92vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div>
            <h2 id="strategy-help-title" className="text-[16px] font-semibold text-foreground">
              Strategy &amp; logic guide
            </h2>
            <p className="mt-1 text-[11px] text-muted">
              How v{STRATEGY_HELP_VERSION} actually trades — gates, sizing, points &amp; ML
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-md border border-border px-3 py-1.5 text-[11px] font-semibold text-muted transition-colors hover:bg-card hover:text-foreground"
            aria-label="Close help"
          >
            Close
          </button>
        </header>

        <nav className="shrink-0 border-b border-border bg-bg/50 px-5 py-2">
          <div className="flex flex-wrap gap-1.5">
            {HELP_SECTIONS.map((s) => (
              <a
                key={s.id}
                href={`#help-${s.id}`}
                className="rounded border border-border/80 bg-card/60 px-2 py-0.5 text-[10px] font-medium text-muted transition-colors hover:border-accent/40 hover:text-foreground"
              >
                {s.title.replace(/ \(config\)/, "")}
              </a>
            ))}
          </div>
        </nav>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="space-y-5">
            {HELP_SECTIONS.map((section) => (
              <HelpSection key={section.id} section={section} />
            ))}
          </div>
          <p className="mt-6 text-center text-[10px] text-muted/80">
            Source: config/config_v25.json + trading_loop.py — update strategyHelp.js when logic changes
          </p>
        </div>
      </div>
    </div>
  );
}
