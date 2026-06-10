import resolveSupervisionAlert, {
  resolveStoppedSupervisionAlert,
} from "../utils/supervision.js";

const SEVERITY_CLASS = {
  success: "border-success/50 bg-success/10 text-success",
  warning: "border-warning/50 bg-warning/10 text-warning",
  danger: "border-danger/50 bg-danger/10 text-danger animate-pulse",
};

export default function SupervisionBanner({
  state,
  shutdownVerification,
  compact = false,
}) {
  const alert = shutdownVerification
    ? resolveStoppedSupervisionAlert(shutdownVerification)
    : resolveSupervisionAlert(state);
  if (!alert) return null;

  const boxClass = SEVERITY_CLASS[alert.severity] || SEVERITY_CLASS.warning;

  const outerClass = compact
    ? "w-full max-w-[360px] rounded-lg border px-3 py-2.5 text-[11px] leading-snug text-left"
    : "border-b px-3 py-2.5 text-[11px] leading-snug sm:px-4 sm:text-xs";

  const icon =
    alert.severity === "success" ? "✓ " : alert.severity === "danger" ? "⚠ " : "◆ ";

  return (
    <div className={[outerClass, boxClass].join(" ")} role="alert">
      <p className="font-semibold">
        {icon}
        {alert.title}
      </p>
      <ul className="mt-1 list-inside list-disc space-y-0.5 opacity-95">
        {(alert.messages || []).slice(0, 4).map((msg) => (
          <li key={msg}>{msg}</li>
        ))}
      </ul>
      <p className="mt-1.5 text-[10px] opacity-80 sm:text-[11px]">
        Launchd watchdog: {alert.launchdWatchdog ? "active" : "missing"}
        {alert.overnightArmed ? " · Overnight armed" : ""}
        {alert.detail ? ` · ${alert.detail}` : ""}
        {alert.showFixHints !== false && (
          <>
            {" · Fix: "}
            <code className="rounded bg-black/20 px-1 py-0.5 font-mono text-[10px]">
              ./scripts/install_launchd.sh
            </code>
            {" or "}
            <code className="rounded bg-black/20 px-1 py-0.5 font-mono text-[10px]">
              PYTHONPATH=src python3 scripts/supervision_check.py --repair
            </code>
          </>
        )}
      </p>
    </div>
  );
}
