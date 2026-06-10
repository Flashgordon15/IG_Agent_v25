/** £1,000/Day Roadmap telemetry helpers — graceful fallbacks for dashboard UI. */

export const APP_VERSION_LABEL = "v29.0";

const MEDALS = ["🥇", "🥈", "🥉"];

export function resolveActiveEpics(state) {
  const fromOrch = state?.orchestrator?.active_epics;
  if (Array.isArray(fromOrch) && fromOrch.length) return fromOrch.filter(Boolean);
  const flat = state?.active_epics;
  if (Array.isArray(flat) && flat.length) return flat.filter(Boolean);
  return [];
}

export function epicShortLabel(epic, labels = {}) {
  if (!epic) return "—";
  if (labels[epic]) return labels[epic];
  const tail = String(epic).split(".").pop() || epic;
  return tail.replace(/\.IP$/i, "").replace(/_/g, " ");
}

export function activeEpicRank(activeEpics, epic) {
  if (!Array.isArray(activeEpics) || !epic) return -1;
  return activeEpics.indexOf(epic);
}

export function medalForRank(rank) {
  if (rank < 0 || rank >= MEDALS.length) return null;
  return MEDALS[rank];
}

export function isEpicRotationMuted(activeEpics, epic) {
  if (!Array.isArray(activeEpics) || activeEpics.length === 0 || !epic) return false;
  return activeEpics.indexOf(epic) === -1;
}

export function resolveSessionStyle(state, viewState) {
  const gates =
    viewState?.health?.gates ||
    state?.health?.gates ||
    [];
  const envGate = gates.find((g) => g?.name === "environment_fitness");
  const fromGate =
    envGate?.value?.factors?.session_style ||
    envGate?.value?.session_style ||
    null;
  return (
    fromGate ||
    state?.session_style ||
    state?.roadmap?.session_style ||
    null
  );
}

export function resolveAppAiHealth(state) {
  if (state == null) {
    return { ready: false, driftOk: null, watchdogActive: null, initializing: true };
  }
  const driftOk = state.supervision_drift_ok;
  const watchdogActive = state.watchdog_active;
  const initializing =
    driftOk == null && watchdogActive == null && !state?.overnight_supervision;
  const ok =
    driftOk !== false &&
    (watchdogActive === true ||
      state?.overnight_supervision?.launchd_watchdog === true);
  return { ready: !initializing, driftOk, watchdogActive, initializing, ok };
}
