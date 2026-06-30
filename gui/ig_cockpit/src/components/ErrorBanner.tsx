import { useCockpit } from "../hooks/CockpitProvider";
import { Button } from "./ui/Controls";

const wsMessages: Record<string, string> = {
  connected: "Live stream active",
  reconnecting: "Reconnecting WebSocket…",
  degraded: "Stream degraded — ticks delayed",
  disconnected: "Offline — REST snapshot only",
};

export default function ErrorBanner() {
  const {
    errors,
    dismissError,
    loading,
    wsState,
    cockpitFailed,
    openBrowserFallback,
  } = useCockpit();

  if (errors.length === 0 && !loading && wsState === "connected") {
    return null;
  }

  return (
    <div className="shrink-0 border-b border-border bg-surface-elevated px-4 py-2">
      {loading && errors.length === 0 && (
        <p className="text-xs text-text-secondary">
          Loading cockpit data from IG Agent backend…
        </p>
      )}
      {wsState !== "connected" && !loading && (
        <p className="mb-1 text-xs text-warning">{wsMessages[wsState]}</p>
      )}
      {cockpitFailed && (
        <div className="mb-1 flex items-center justify-between gap-3 rounded border border-warning/40 bg-warning/10 px-3 py-2">
          <p className="text-xs text-warning">
            Cockpit boot timed out — agent may still be running.
          </p>
          <Button variant="accent" className="py-0.5 text-[10px]" onClick={openBrowserFallback}>
            Open browser dashboard
          </Button>
        </div>
      )}
      {errors.map((err, index) => (
        <div
          key={`${err}-${index}`}
          className="mb-1 flex items-start justify-between gap-3 rounded border border-danger/40 bg-danger/10 px-3 py-2 last:mb-0"
        >
          <p className="text-xs text-danger">{err}</p>
          <button
            type="button"
            className="shrink-0 text-[10px] uppercase tracking-wide text-text-secondary hover:text-text"
            onClick={() => dismissError(index)}
          >
            Dismiss
          </button>
        </div>
      ))}
    </div>
  );
}
