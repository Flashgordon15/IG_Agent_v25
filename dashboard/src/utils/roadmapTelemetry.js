/** £1,000/Day Roadmap telemetry helpers — graceful fallbacks for dashboard UI. */

export const APP_VERSION_LABEL = "v29.1";

const MEDALS = ["🥇", "🥈", "🥉"];

export function resolveGateRelaxations(state) {
  const block = state?.gate_relaxations;
  return block && typeof block === "object" ? block : null;
}

export function resolveEffectivePolicy(state) {
  const block = state?.effective_policy;
  return block && typeof block === "object" ? block : null;
}

export function isRotationFilterBypassed(state) {
  const relax = resolveGateRelaxations(state);
  if (relax?.disable_rotation_filter === true) return true;
  if (relax?.demo_soak_mode === true && relax?.disable_rotation_filter !== false) {
    return true;
  }
  return false;
}

const TOP_ROTATION_DISPLAY_SLOTS = 5;

export function resolveInitCleared(state) {
  if (state?.init_force_cleared === true) return true;
  const loopsRunning = state?.trading_loops_running !== false;
  const quotesLive =
    state?.quotes_fresh === true || Number(state?.quotes_fresh_count ?? 0) > 0;
  const liveSec = Number(state?.init_live_sec ?? 0);
  return loopsRunning && quotesLive && liveSec >= 90;
}

export function resolveActiveEpics(state) {
  const fromOrch = state?.orchestrator?.active_epics;
  if (Array.isArray(fromOrch) && fromOrch.length) return fromOrch.filter(Boolean);
  const flat = state?.active_epics;
  if (Array.isArray(flat) && flat.length) return flat.filter(Boolean);
  if (resolveInitCleared(state)) {
    const enabled = state?.enabled_epics;
    if (Array.isArray(enabled) && enabled.length) {
      return enabled.filter(Boolean).slice(0, TOP_ROTATION_DISPLAY_SLOTS);
    }
    const markets = state?.markets;
    if (markets && typeof markets === "object") {
      const keys = Object.keys(markets).filter(Boolean);
      if (keys.length) return keys.slice(0, TOP_ROTATION_DISPLAY_SLOTS);
    }
  }
  return [];
}

export { TOP_ROTATION_DISPLAY_SLOTS };

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

export function isEpicRotationMuted(activeEpics, epic, state = null) {
  if (isRotationFilterBypassed(state)) return false;
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
  const initCleared = resolveInitCleared(state);
  const initializing = !initCleared;
  const ok =
    !initializing &&
    driftOk !== false &&
    (watchdogActive === true ||
      state?.overnight_supervision?.launchd_watchdog === true);
  return { ready: !initializing, driftOk, watchdogActive, initializing, ok, initCleared };
}
