import * as Progress from "@radix-ui/react-progress";
import * as Separator from "@radix-ui/react-separator";
import { useCockpit } from "../hooks/CockpitProvider";
import type { StatusLevel } from "../types/cockpit";
import { Badge } from "./ui/Badge";
import { cn } from "../lib/utils";
import { Tip } from "./ui/Controls";

const TOOLTIPS: Record<string, string> = {
  auth: "Session identity and attach readiness from gui_status",
  feeds: "Hub quote freshness and stream transport status",
  routing: "Unified execution route cache warm-up progress",
  execution: "Per-epic pipeline state and order valve context",
  risk: "Regime risk envelope and hard enforcement posture",
  ledger: "Session lock and ledger attach identity",
};

const dotClass: Record<StatusLevel, string> = {
  ok: "status-dot-ok",
  warn: "status-dot-warn",
  error: "status-dot-error",
};

const badgeVariant: Record<StatusLevel, "success" | "warning" | "danger"> = {
  ok: "success",
  warn: "warning",
  error: "danger",
};

export default function StatusMatrix() {
  const { statusRows, panelFocus } = useCockpit();
  const focused = panelFocus === "strategy";

  return (
    <div
      className={cn(
        "panel flex h-full flex-col transition-all duration-300 gpu-layer",
        focused && "ring-1 ring-accent/50",
      )}
      id="panel-strategy"
    >
      <div className="panel-header">Platform Ready</div>
      <ul className="flex-1 overflow-y-auto p-2">
        {statusRows.map((row, index) => (
          <li key={row.key} className="animate-fade-in">
            <div className="flex items-start gap-2 rounded px-2 py-2 hover:bg-surface-elevated/50 transition-colors duration-300">
              <span
                className={cn(
                  "status-dot mt-1.5 transition-all duration-500",
                  dotClass[row.status],
                  row.status === "ok" && "animate-pulse-soft",
                )}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <Tip label={TOOLTIPS[row.key] ?? row.label}>
                    <span className="cursor-help text-xs font-medium text-text">
                      {row.label}
                    </span>
                  </Tip>
                  <Badge variant={badgeVariant[row.status]}>{row.status}</Badge>
                </div>
                <p className="mt-0.5 text-[11px] text-text-secondary">{row.detail}</p>
                {row.hint && (
                  <p className="mt-1 text-[10px] italic text-muted">{row.hint}</p>
                )}
                {row.progress !== undefined && (
                  <Progress.Root
                    className="relative mt-2 h-1 w-full overflow-hidden rounded-full bg-surface-elevated"
                    value={row.progress}
                  >
                  <Progress.Indicator
                    className="h-full bg-accent transition-transform duration-700 ease-out"
                    style={{ transform: `translateX(-${100 - row.progress}%)` }}
                  />
                  </Progress.Root>
                )}
                {row.updatedAt && (
                  <p className="mt-1 font-mono text-[9px] text-muted">
                    {new Date(row.updatedAt).toLocaleTimeString()}
                  </p>
                )}
              </div>
            </div>
            {index < statusRows.length - 1 && (
              <Separator.Root className="mx-2 h-px bg-border" />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
