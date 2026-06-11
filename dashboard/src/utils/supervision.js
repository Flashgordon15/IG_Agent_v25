/** Human-readable supervision drift messages for dashboard banners. */

const ISSUE_LABELS = {
  overnight_armed_but_launchd_watchdog_missing:
    "Overnight armed but launchd watchdog is missing — run ./scripts/install_launchd.sh",
  agent_running_without_watchdog:
    "Agent is running without watchdog supervision",
  duplicate_main_py_processes:
    "Multiple agent processes detected — only one main.py should run",
};

const WARNING_LABELS = {
  manual_stop_active_agent_down:
    "Manual stop active — watchdog will not auto-restart for ~10 min",
  manual_stop_active_while_agent_running:
    "Manual stop flag set while agent is still running",
  launchd_watchdog_job_loaded_but_process_not_detected:
    "Launchd watchdog job loaded but process not detected — check watchdog_launchd.log",
};

export const STOP_SUPERVISION_KEY = "ig_agent_stop_supervision";
const STOP_SUPERVISION_TTL_MS = 600_000;

function labelFor(code, map) {
  if (!code || typeof code !== "string") return null;
  const base = code.split(":")[0];
  if (map[base]) return map[base];
  if (map[code]) return map[code];
  return code.replace(/_/g, " ");
}

export function persistStopSupervision(snapshot) {
  if (typeof window === "undefined" || !snapshot || typeof snapshot !== "object") {
    return;
  }
  try {
    window.sessionStorage.setItem(
      STOP_SUPERVISION_KEY,
      JSON.stringify({ ts: Date.now(), ...snapshot }),
    );
  } catch {
    /* ignore quota / private mode */
  }
}

export function loadStopSupervision() {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(STOP_SUPERVISION_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object") return null;
    const age = Date.now() - Number(data.ts || 0);
    if (age < 0 || age > STOP_SUPERVISION_TTL_MS) {
      window.sessionStorage.removeItem(STOP_SUPERVISION_KEY);
      return null;
    }
    return data;
  } catch {
    return null;
  }
}

export function buildStoppedSupervisionState(shutdownVerification) {
  const final = shutdownVerification?.final || {};
  const cleanup = shutdownVerification?.cleanup || [];
  const cached = loadStopSupervision();

  const launchdCheck = cleanup.find((c) => c.label === "Launchd supervision");
  const launchdFromCleanup = launchdCheck?.ok === true;

  const overnight =
    final.overnight_supervision ||
    cached?.overnight_supervision ||
    {};
  const launchdWatchdog = Boolean(
    overnight.launchd_watchdog ?? launchdFromCleanup ?? cached?.overnight_supervision?.launchd_watchdog,
  );

  const drift = final.supervision_drift || cached?.supervision_drift || {};
  const warnings = [
    ...(final.supervision_warnings || cached?.supervision_warnings || []),
  ];
  if (!warnings.includes("manual_stop_active_agent_down")) {
    warnings.push("manual_stop_active_agent_down");
  }

  const driftOk =
    final.supervision_drift_ok ??
    cached?.supervision_drift_ok ??
    (launchdWatchdog && (drift.ok !== false));

  return {
    stopped_screen: true,
    supervision_drift_ok: driftOk !== false,
    supervision_drift: drift,
    supervision_warnings: warnings,
    overnight_supervision: {
      ...overnight,
      launchd_watchdog: launchdWatchdog,
    },
    overnight_armed: final.overnight_armed ?? cached?.overnight_armed,
    watchdog_active: launchdWatchdog,
  };
}

export function resolveSupervisionAlert(state) {
  if (!state || state.supervision_drift_ok !== false) {
    const warnings = state?.supervision_warnings || [];
    if (warnings.length > 0) {
      return {
        severity: "warning",
        title: "Supervision warning",
        messages: warnings.map((w) => labelFor(w, WARNING_LABELS)),
        launchdWatchdog: state?.overnight_supervision?.launchd_watchdog,
        overnightArmed: state?.overnight_armed,
        showFixHints: true,
      };
    }
    return null;
  }

  const drift = state.supervision_drift || {};
  const issues = drift.issues || [];
  const warnings = state.supervision_warnings || drift.warnings || [];

  return {
    severity: "danger",
    title: "Supervision drift detected",
    messages: [
      ...issues.map((i) => labelFor(i, ISSUE_LABELS)),
      ...warnings.map((w) => labelFor(w, WARNING_LABELS)),
    ].filter(Boolean),
    launchdWatchdog: state?.overnight_supervision?.launchd_watchdog,
    overnightArmed: state?.overnight_armed,
    detail: state?.overnight_supervision?.launchd_detail,
    showFixHints: true,
  };
}

/** Banner content for the post-Stop Agent screen (agent down, verify server / session cache). */
export function resolveStoppedSupervisionAlert(shutdownVerification) {
  const state = buildStoppedSupervisionState(shutdownVerification);
  if (!state) return null;

  if (state.supervision_drift_ok === false) {
    return resolveSupervisionAlert(state);
  }

  if (state.overnight_supervision?.launchd_watchdog) {
    return {
      severity: "success",
      title: "Launchd supervision preserved",
      messages: [
        "Watchdog stays loaded after Stop Agent — Safe to Leave survives this stop",
        "Manual stop active ~10 min — watchdog will not auto-restart the agent",
        ...(state.overnight_armed ? ["Overnight armed — you may close Cursor and this tab"] : []),
      ],
      launchdWatchdog: true,
      overnightArmed: state.overnight_armed,
      detail: state.overnight_supervision?.launchd_detail,
      showFixHints: false,
    };
  }

  return {
    severity: "danger",
    title: "No launchd supervision after stop",
    messages: [
      "Agent stopped without launchd watchdog — run install_launchd.sh before leaving unattended",
      "Manual stop active ~10 min — no auto-restart until you relaunch",
    ],
    launchdWatchdog: false,
    overnightArmed: state.overnight_armed,
    showFixHints: true,
  };
}

export default resolveSupervisionAlert;
