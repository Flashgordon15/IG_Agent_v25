/** Electron main-process boot phase fan-out (daemon supervisor). */

const bootPhaseListeners = new Set();

export function subscribeBootPhase(callback) {
  if (typeof callback !== "function") {
    return () => {};
  }
  bootPhaseListeners.add(callback);

  if (typeof window !== "undefined" && window.apexIPC?.onBootPhase) {
    window.apexIPC.onBootPhase((payload) => {
      if (payload != null) notifyBootPhaseListeners(payload);
    });
    return () => bootPhaseListeners.delete(callback);
  }

  return () => bootPhaseListeners.delete(callback);
}

function notifyBootPhaseListeners(payload) {
  for (const cb of bootPhaseListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apexBootPhase listener error", err);
    }
  }
}

export function engageBootPhaseBridge() {
  if (typeof window === "undefined" || !window.apexIPC?.onBootPhase) return false;
  window.apexIPC.onBootPhase((payload) => {
    if (payload != null) notifyBootPhaseListeners(payload);
  });
  return true;
}
