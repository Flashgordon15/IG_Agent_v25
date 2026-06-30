import * as Dialog from "@radix-ui/react-dialog";
import { useState } from "react";
import { useCockpit } from "../hooks/CockpitProvider";
import { API_BASE } from "../lib/api";
import { cn } from "../lib/utils";
import { Badge } from "./ui/Badge";
import { Button, Tip } from "./ui/Controls";

const wsLabel: Record<string, string> = {
  connected: "Live",
  reconnecting: "Reconnecting…",
  degraded: "Degraded",
  disconnected: "Offline",
};

const latencyClass = {
  ok: "text-success",
  warn: "text-warning",
  error: "text-danger",
};

export default function TopBar() {
  const {
    topBar,
    refresh,
    loading,
    pauseTrading,
    resumeTrading,
    suppressOrders,
    setPanelFocus,
    orderValve,
  } = useCockpit();
  const [settingsOpen, setSettingsOpen] = useState(false);

  const pnlText =
    topBar.pnlGbp === null
      ? "—"
      : `${topBar.pnlGbp >= 0 ? "+" : ""}£${topBar.pnlGbp.toFixed(2)}`;
  const pnlClass =
    topBar.pnlGbp === null
      ? "text-text-secondary"
      : topBar.pnlGbp >= 0
        ? "text-success"
        : "text-danger";

  const wsDotClass =
    topBar.wsState === "connected"
      ? "status-dot-ok"
      : topBar.wsState === "degraded"
        ? "status-dot-warn animate-pulse-soft"
        : topBar.wsState === "reconnecting"
          ? "status-dot-warn animate-pulse-soft"
          : "status-dot-error";

  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface/95 px-4 backdrop-blur-sm transition-all duration-300">
      <div className="flex items-center gap-3">
        <h1 className="text-sm font-semibold tracking-wide text-text">
          IG Cockpit
        </h1>
        <Badge variant="accent" className="transition-all duration-300">
          {topBar.regime}
        </Badge>
        <Badge variant="neutral" className="transition-all duration-300">
          {topBar.strategy}
        </Badge>
        <Badge
          variant={
            orderValve === "open"
              ? "success"
              : orderValve === "warming"
                ? "warning"
                : "danger"
          }
          className={orderValve === "suppressed" ? "animate-pulse-soft" : ""}
        >
          Valve {orderValve}
        </Badge>
      </div>

      <div className="flex items-center gap-5">
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-text-secondary">
            Session P&amp;L
          </div>
          <div className={`font-mono text-sm transition-all duration-300 ${pnlClass}`}>
            {pnlText}
          </div>
        </div>

        <div className="hidden text-right sm:block">
          <div className="text-[10px] uppercase tracking-wider text-text-secondary">
            Latency · Feeds
          </div>
          <div className="font-mono text-xs">
            <span className={latencyClass[topBar.latencyLevel]}>
              {topBar.latencyMs !== null ? `${topBar.latencyMs}ms` : "—"}
            </span>
            <span className="text-text-secondary"> · </span>
            <span
              className={cn(
                topBar.feedHealth === "ok"
                  ? "text-success"
                  : topBar.feedHealth === "warn"
                    ? "text-warning animate-pulse-soft"
                    : "text-danger animate-pulse-soft",
              )}
            >
              {topBar.feedStatus}
            </span>
          </div>
        </div>

        <Tip label={`WebSocket: ${topBar.wsState}`}>
          <div className="flex items-center gap-2">
            <span className={cn("status-dot", wsDotClass)} />
            <span className="text-xs text-text-secondary">
              {wsLabel[topBar.wsState] ?? topBar.sessionStatus}
            </span>
          </div>
        </Tip>

        <div className="flex items-center gap-1.5">
          <Tip
            label={
              topBar.tradingPaused
                ? "Resume trading (POST /api/start)"
                : "Pause trading (POST /api/stop)"
            }
          >
            <Button
              variant={topBar.tradingPaused ? "accent" : "default"}
              disabled={loading}
              onClick={() =>
                topBar.tradingPaused ? resumeTrading() : pauseTrading()
              }
            >
              {topBar.tradingPaused ? "Resume" : "Pause"}
            </Button>
          </Tip>
          <Tip label="Emergency suppress (POST /api/emergency_stop)">
            <Button variant="danger" disabled={loading} onClick={() => suppressOrders()}>
              Suppress
            </Button>
          </Tip>
          <Button variant="ghost" onClick={() => setPanelFocus("logs")}>
            Logs ⌘L
          </Button>
          <Button variant="ghost" onClick={() => setSettingsOpen(true)}>
            Settings
          </Button>
          <Button variant="default" disabled={loading} onClick={refresh}>
            Refresh
          </Button>
        </div>
      </div>

      <Dialog.Root open={settingsOpen} onOpenChange={setSettingsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(420px,90vw)] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-surface p-4 shadow-xl animate-slide-in">
            <Dialog.Title className="text-sm font-semibold text-text">
              Cockpit Settings
            </Dialog.Title>
            <Dialog.Description className="mt-2 text-xs text-text-secondary">
              Read-only connection info. Agent control uses non-blocking POST calls.
            </Dialog.Description>
            <dl className="mt-4 space-y-2 font-mono text-[11px] text-text-secondary">
              <div className="flex justify-between gap-4">
                <dt>API</dt>
                <dd className="text-text">{API_BASE}</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt>WebSocket</dt>
                <dd className="text-text">
                  {API_BASE.replace(/^http/, "ws")}/ws/stream
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt>WS state</dt>
                <dd className="text-text">{topBar.wsState}</dd>
              </div>
            </dl>
            <div className="mt-4 flex justify-end">
              <Dialog.Close asChild>
                <Button>Close</Button>
              </Dialog.Close>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </header>
  );
}
