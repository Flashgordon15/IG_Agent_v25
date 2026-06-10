import {
  APP_VERSION_LABEL,
  epicShortLabel,
  resolveActiveEpics,
} from "../utils/roadmapTelemetry.js";

const RANK_BADGE_STYLES = [
  "border-amber-400/70 bg-amber-500/20 text-amber-50 ring-1 ring-amber-400/30",
  "border-slate-200/50 bg-slate-300/15 text-slate-50 ring-1 ring-slate-300/25",
  "border-orange-500/50 bg-orange-600/15 text-orange-50 ring-1 ring-orange-500/25",
];

const RANK_MEDALS = ["🥇", "🥈", "🥉"];

export default function ActiveGatesRibbon({ state }) {
  const activeEpics = resolveActiveEpics(state);
  const labels = state?.instrument_labels || {};

  if (!activeEpics.length) {
    return (
      <div className="border-b border-border/80 bg-card/40 px-3 py-2 text-[10px] text-muted sm:px-4 sm:text-[11px]">
        <span className="font-semibold uppercase tracking-wide text-muted/80">
          Active Gates:
        </span>{" "}
        <span className="inline-flex items-center gap-1 rounded-full border border-border bg-card/60 px-2.5 py-1 text-muted">
          INITIALIZING…
        </span>
        <span className="ml-2 text-[9px] text-muted/70">{APP_VERSION_LABEL}</span>
      </div>
    );
  }

  return (
    <div className="border-b border-border/80 bg-card/40 px-3 py-2 sm:px-4">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 text-[10px] sm:text-[11px]">
        <span className="shrink-0 font-semibold uppercase tracking-wide text-muted/80">
          Active Gates:
        </span>
        {activeEpics.slice(0, 3).map((epic, idx) => (
          <span
            key={epic}
            role="status"
            aria-label={`Active gate rank ${idx + 1}: ${epicShortLabel(epic, labels)}`}
            className={[
              "inline-flex shrink-0 select-none items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-bold uppercase tracking-wide",
              RANK_BADGE_STYLES[idx] ?? RANK_BADGE_STYLES[2],
            ].join(" ")}
          >
            <span aria-hidden>{RANK_MEDALS[idx] ?? RANK_MEDALS[2]}</span>
            {epicShortLabel(epic, labels)}
          </span>
        ))}
        <span className="ml-auto text-[9px] font-medium uppercase tracking-wider text-muted/70">
          {APP_VERSION_LABEL}
        </span>
      </div>
    </div>
  );
}
