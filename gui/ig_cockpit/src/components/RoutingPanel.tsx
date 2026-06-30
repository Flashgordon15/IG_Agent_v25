import * as ScrollArea from "@radix-ui/react-scroll-area";
import { useCockpit } from "../hooks/CockpitProvider";
import type { RouteState } from "../types/cockpit";
import { Badge } from "./ui/Badge";
import { cn } from "../lib/utils";

const stateBadge: Record<
  RouteState,
  "success" | "warning" | "danger" | "neutral" | "accent"
> = {
  active: "success",
  warming: "warning",
  degraded: "danger",
  idle: "neutral",
};

export default function RoutingPanel() {
  const { routes, panelFocus } = useCockpit();
  const focused = panelFocus === "routing";

  return (
    <div
      id="panel-routing"
      className={cn(
        "panel flex min-h-0 flex-1 flex-col overflow-hidden transition-all duration-300 gpu-layer",
        focused && "ring-1 ring-accent/50",
      )}
    >
      <div className="panel-header">Execution Routing</div>
      <ScrollArea.Root className="min-h-0 flex-1">
        <ScrollArea.Viewport className="h-full w-full p-2">
          {routes.length === 0 ? (
            <p className="px-2 text-xs text-text-secondary">Routes warming…</p>
          ) : (
            <ul className="space-y-2">
              {routes.map((route) => (
                <li
                  key={route.epic}
                  className="animate-fade-in rounded border border-border bg-surface-elevated/40 px-2 py-2 transition-colors hover:border-accent/30"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs font-medium text-text">
                      {route.epic}
                    </span>
                    <Badge variant={stateBadge[route.state]}>{route.state}</Badge>
                  </div>
                  <p className="mt-1 font-mono text-[10px] text-text-secondary">
                    {route.path} · {route.confidence}%
                  </p>
                  <div className="mt-1.5 flex flex-wrap gap-2 text-[10px] text-muted">
                    <span>{route.venue}</span>
                    {route.latencyMs !== null && <span>{route.latencyMs}ms</span>}
                    <span>{route.fillQuality}</span>
                    {route.slippageBps !== null && (
                      <span>
                        {route.slippageTrend === "up" ? "↑" : route.slippageTrend === "down" ? "↓" : "→"}{" "}
                        {route.slippageBps} bps
                      </span>
                    )}
                  </div>
                  <p className="mt-1 line-clamp-2 text-[10px] text-text-secondary">
                    {route.reason}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </ScrollArea.Viewport>
        <ScrollArea.Scrollbar
          orientation="vertical"
          className="flex w-1.5 touch-none select-none bg-surface p-0.5"
        >
          <ScrollArea.Thumb className="relative flex-1 rounded-full bg-border" />
        </ScrollArea.Scrollbar>
      </ScrollArea.Root>
    </div>
  );
}
