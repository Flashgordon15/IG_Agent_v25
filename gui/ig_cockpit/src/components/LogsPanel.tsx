import * as ScrollArea from "@radix-ui/react-scroll-area";
import { useEffect, useRef } from "react";
import { useCockpit } from "../hooks/CockpitProvider";
import type { LogSubsystem } from "../types/cockpit";
import { levelColor } from "../lib/logStream";
import { Button } from "./ui/Controls";
import { cn } from "../lib/utils";

const FILTERS: { id: LogSubsystem; label: string }[] = [
  { id: "all", label: "All" },
  { id: "feeds", label: "Feeds" },
  { id: "routing", label: "Routing" },
  { id: "sizing", label: "Sizing" },
  { id: "governance", label: "Gov" },
  { id: "execution", label: "Exec" },
];

export default function LogsPanel() {
  const {
    logLines,
    logFilter,
    setLogFilter,
    logsPaused,
    setLogsPaused,
    panelFocus,
  } = useCockpit();
  const bottomRef = useRef<HTMLDivElement>(null);
  const focused = panelFocus === "logs";

  useEffect(() => {
    if (!logsPaused) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [logLines, logsPaused]);

  return (
    <div
      id="panel-logs"
      className={cn(
        "panel flex min-h-0 flex-[1.2] flex-col overflow-hidden gpu-layer transition-all duration-300",
        focused && "ring-1 ring-accent/50",
      )}
    >
      <div className="panel-header">
        <span>Live Log</span>
        <div className="flex items-center gap-2">
          <Button
            variant={logsPaused ? "accent" : "ghost"}
            className="py-0.5 text-[10px]"
            onClick={() => setLogsPaused(!logsPaused)}
          >
            {logsPaused ? "Resume scroll" : "Pause"}
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-border px-2 py-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => setLogFilter(f.id)}
            className={cn(
              "rounded px-2 py-0.5 text-[10px] transition-colors",
              logFilter === f.id
                ? "bg-accent/20 text-accent"
                : "text-text-secondary hover:text-text",
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      <ScrollArea.Root className="min-h-0 flex-1">
        <ScrollArea.Viewport className="h-full w-full p-2">
          <pre className="font-mono text-[11px] leading-relaxed">
            {logLines.map((line) => (
              <div
                key={line.id}
                className={cn("animate-fade-in", levelColor(line.level))}
              >
                <span className="text-muted">[{line.ts}]</span>{" "}
                <span className="uppercase text-[9px] text-muted">
                  {line.subsystem}
                </span>{" "}
                {line.message}
              </div>
            ))}
            <div ref={bottomRef} />
          </pre>
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
