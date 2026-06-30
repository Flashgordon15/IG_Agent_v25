/**
 * Trading path panel — signals, validation, orders, IG responses, lifecycle events.
 */

import { memo, useCallback, useEffect, useState } from "react";
import { fetchTradeEvents, fetchTradeState } from "../lib/api";
import { cn } from "../lib/utils";

interface TradeEvent {
  type: string;
  ts: string;
  payload?: Record<string, unknown>;
}

function TradingPathPanelInner() {
  const [events, setEvents] = useState<TradeEvent[]>([]);
  const [lifecycleCount, setLifecycleCount] = useState(0);
  const [stopsCount, setStopsCount] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const [state, ev] = await Promise.all([
        fetchTradeState(),
        fetchTradeEvents(30),
      ]);
      const lc = state.lifecycle as { active?: Record<string, unknown> };
      setLifecycleCount(Object.keys(lc?.active ?? {}).length);
      const stops = state.stops as { count?: number };
      setStopsCount(stops?.count ?? 0);
      setEvents(((ev.events as TradeEvent[]) ?? []).slice(0, 12));
    } catch {
      /* non-blocking */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 2500);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <div className="rounded-lg border border-border/60 bg-panel/50 p-2">
      <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
        Trading Path
      </h3>
      <div className="mb-2 flex gap-3 text-[10px] text-slate-400">
        <span>Active lifecycle: {lifecycleCount}</span>
        <span>Trailing stops: {stopsCount}</span>
      </div>
      <ul className="max-h-32 space-y-0.5 overflow-y-auto font-mono text-[9px]">
        {events.length === 0 ? (
          <li className="text-muted">No events yet</li>
        ) : (
          events.map((e, i) => (
            <li
              key={`${e.ts}-${i}`}
              className={cn(
                "truncate",
                e.type.includes("rejection") ? "text-red-400" : "text-slate-300",
              )}
            >
              {e.ts.slice(11, 19)} {e.type}{" "}
              {e.payload?.epic ? String(e.payload.epic) : ""}
              {e.payload?.from && e.payload?.to
                ? ` ${String(e.payload.from)}→${String(e.payload.to)}`
                : ""}
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

export default memo(TradingPathPanelInner);
