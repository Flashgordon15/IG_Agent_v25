import { useCockpit } from "../hooks/CockpitProvider";
import type { BootStageRow, BootStepStatus, BootSubsystemRow } from "../types/cockpit";

function statusDot(status: BootStepStatus): string {
  if (status === "ok") return "status-dot-ok";
  if (status === "failed") return "status-dot-error";
  if (status === "healing") return "status-dot-warn animate-pulse-soft";
  if (status === "running" || status === "degraded") return "status-dot-warn";
  return "status-dot-warn opacity-50";
}

function StageRow({ row }: { row: BootStageRow }) {
  return (
    <div className="flex items-center justify-between gap-2 text-left text-[11px]">
      <div className="flex min-w-0 items-center gap-2">
        <span className={`status-dot shrink-0 ${statusDot(row.status)}`} />
        <span className="truncate text-text-secondary">
          <span className="font-mono text-muted">{row.id}</span> {row.label}
        </span>
      </div>
      <span className="shrink-0 uppercase tracking-wider text-muted">{row.status}</span>
    </div>
  );
}

function SubsystemRow({ row }: { row: BootSubsystemRow }) {
  return (
    <div className="flex items-center justify-between gap-2 text-left text-[10px]">
      <div className="flex min-w-0 items-center gap-2">
        <span className={`status-dot shrink-0 ${statusDot(row.status)}`} />
        <span className="truncate text-muted">{row.label}</span>
      </div>
      <span className="shrink-0 text-muted">
        {row.healAction ? `heal: ${row.healAction}` : row.status}
        {row.retryCount ? ` · r${row.retryCount}` : ""}
      </span>
    </div>
  );
}

export default function SplashScreen() {
  const { loading, ready, cockpitUsable, readinessLabel, wsState, bootStatus } =
    useCockpit();

  const tradeReady = bootStatus?.tradeReady ?? false;
  const currentStage = bootStatus?.currentStage ?? "A";
  const elapsed = bootStatus?.bootElapsedSec ?? 0;
  const eta = bootStatus?.estimatedReadySec;

  const message = tradeReady
    ? "Trade ready — all subsystems online"
    : loading
      ? "Connecting to IG Agent backend…"
      : ready
        ? "Platform ready — confirming execution plane"
        : cockpitUsable
          ? `${readinessLabel} — live data available`
          : readinessLabel !== "Connecting…"
            ? `Stage ${currentStage}: ${readinessLabel}…`
            : "Platform warming…";

  return (
    <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-bg/95 backdrop-blur-md">
      <div className="animate-slide-in w-full max-w-md px-6 text-center">
        <div className="mb-4 inline-flex h-12 w-12 animate-pulse items-center justify-center rounded-full border border-accent/40 bg-accent/10">
          <span className="text-lg font-semibold text-accent">IG</span>
        </div>
        <h2 className="text-lg font-semibold tracking-wide text-text">IG Cockpit</h2>
        <p className="mt-2 text-sm text-text-secondary">{message}</p>

        <div className="mt-3 flex justify-center gap-3 text-[10px] uppercase tracking-wider text-muted">
          <span>Stage {currentStage}</span>
          <span>{elapsed.toFixed(0)}s elapsed</span>
          {eta != null && !tradeReady ? <span>~{Math.ceil(eta)}s ETA</span> : null}
        </div>

        {bootStatus && bootStatus.stages.length > 0 ? (
          <div className="mt-5 max-h-36 space-y-1 overflow-y-auto rounded-lg border border-border/60 bg-panel/40 p-3">
            {bootStatus.stages.map((row) => (
              <StageRow key={row.id} row={row} />
            ))}
          </div>
        ) : null}

        {bootStatus?.startupDiagnostics && bootStatus.startupDiagnostics.length > 0 ? (
          <div className="mt-3 max-h-32 space-y-1 overflow-y-auto rounded-lg border border-border/50 bg-panel/30 p-3">
            <p className="mb-1 text-[9px] uppercase tracking-wider text-muted">
              Startup diagnostics
            </p>
            {bootStatus.startupDiagnostics.map((row) => (
              <div
                key={row.key}
                className="flex items-center justify-between gap-2 text-left text-[10px]"
              >
                <div className="flex min-w-0 items-center gap-2">
                  <span
                    className={`status-dot shrink-0 ${row.ok ? "status-dot-ok" : "status-dot-warn opacity-60"}`}
                  />
                  <span className="truncate text-muted">{row.label}</span>
                </div>
                <span className="shrink-0 uppercase text-muted">
                  {row.ok ? "ok" : "wait"}
                </span>
              </div>
            ))}
          </div>
        ) : null}

        {bootStatus && bootStatus.subsystems.some((s) => s.status !== "ok" && s.status !== "pending") ? (
          <div className="mt-2 max-h-24 space-y-1 overflow-y-auto rounded-lg border border-border/40 bg-panel/30 p-2">
            {bootStatus.subsystems
              .filter((s) => s.status !== "ok" && s.status !== "pending")
              .map((row) => (
                <SubsystemRow key={row.id} row={row} />
              ))}
          </div>
        ) : null}

        {bootStatus?.blockers && bootStatus.blockers.length > 0 && !tradeReady ? (
          <p className="mt-2 text-[10px] text-warn">
            Blockers: {bootStatus.blockers.slice(0, 3).join(", ")}
          </p>
        ) : null}

        <div className="mt-4 flex justify-center gap-2">
          <span
            className={`status-dot ${
              tradeReady || cockpitUsable ? "status-dot-ok" : ready ? "status-dot-ok" : "status-dot-warn"
            }`}
          />
          <span
            className={`status-dot ${
              wsState === "connected"
                ? "status-dot-ok"
                : wsState === "reconnecting"
                  ? "status-dot-warn animate-pulse-soft"
                  : "status-dot-warn"
            }`}
          />
          <span className="text-[10px] uppercase tracking-wider text-muted">
            {tradeReady ? "TRADE READY" : cockpitUsable ? "LIVE" : ready ? "GUI" : "BOOT"} · {wsState}
          </span>
        </div>
      </div>
    </div>
  );
}
