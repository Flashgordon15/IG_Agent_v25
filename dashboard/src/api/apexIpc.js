/**
 * Apex desktop IPC bridge — primary tick transport for Electron shell.
 * All live ticks route through preload apexIPC (Unix domain socket); HTTP poll is dev-only fallback.
 */

const tickListeners = new Set();
const statusListeners = new Set();
const warmupListeners = new Set();
const ledgerListeners = new Set();
const storyListeners = new Set();
let nativeBridgeEngaged = false;

/** Single-shot preload bridge — zero-copy IPC on file:// desktop shell. */
function engageNativeIpcBridge() {
  if (nativeBridgeEngaged) return true;
  if (typeof window === "undefined") return false;
  if (window.apexIPC && typeof window.apexIPC.onTick === "function") {
    console.log("[APEX ENGINE] Native Zero-Copy IPC Preload Bridge engaged.");
    window.apexIPC.onTick((tickPayload) => {
      if (tickPayload != null) {
        notifyTickListeners(tickPayload);
      }
    });
    if (typeof window.apexIPC.onStatus === "function") {
      window.apexIPC.onStatus((status) => {
        notifyStatusListeners(status);
      });
    }
    if (typeof window.apexIPC.onWarmup === "function") {
      window.apexIPC.onWarmup((warmup) => {
        notifyWarmupListeners(warmup);
      });
    }
    if (typeof window.apexIPC.onLedger === "function") {
      window.apexIPC.onLedger((packet) => {
        notifyLedgerListeners(packet);
      });
    }
    if (typeof window.apexIPC.onStory === "function") {
      window.apexIPC.onStory((packet) => {
        notifyStoryListeners(packet);
      });
    }
    nativeBridgeEngaged = true;
    return true;
  }
  return false;
}

export function isApexDesktopShell() {
  return typeof window !== "undefined" && Boolean(window.apexIPC?.isDesktopShell?.());
}

function notifyTickListeners(payload) {
  for (const cb of tickListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apexIpc tick listener error", err);
    }
  }
}

function notifyStatusListeners(payload) {
  for (const cb of statusListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apexIpc status listener error", err);
    }
  }
}

function notifyWarmupListeners(payload) {
  for (const cb of warmupListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apexIpc warmup listener error", err);
    }
  }
}

export function subscribeWarmup(callback) {
  if (typeof callback !== "function") {
    return () => {};
  }
  warmupListeners.add(callback);

  if (engageNativeIpcBridge()) {
    return () => warmupListeners.delete(callback);
  }

  return () => warmupListeners.delete(callback);
}

function notifyLedgerListeners(payload) {
  for (const cb of ledgerListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apexIpc ledger listener error", err);
    }
  }
}

export function subscribeLedger(callback) {
  if (typeof callback !== "function") {
    return () => {};
  }
  ledgerListeners.add(callback);

  if (typeof window !== "undefined" && window.apexIPC?.onLedger) {
    window.apexIPC.onLedger((packet) => {
      if (packet != null) notifyLedgerListeners(packet);
    });
    return () => ledgerListeners.delete(callback);
  }

  return () => ledgerListeners.delete(callback);
}

function notifyStoryListeners(payload) {
  for (const cb of storyListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apexIpc story listener error", err);
    }
  }
}

export function subscribeStory(callback) {
  if (typeof callback !== "function") {
    return () => {};
  }
  storyListeners.add(callback);

  if (typeof window !== "undefined" && window.apexIPC?.onStory) {
    window.apexIPC.onStory((packet) => {
      if (packet != null) notifyStoryListeners(packet);
    });
    return () => storyListeners.delete(callback);
  }

  return () => storyListeners.delete(callback);
}

export function subscribeTicks(callback) {
  if (typeof callback !== "function") {
    return () => {};
  }
  tickListeners.add(callback);

  if (engageNativeIpcBridge()) {
    return () => tickListeners.delete(callback);
  }

  return () => tickListeners.delete(callback);
}

export function subscribeIpcStatus(callback) {
  if (typeof callback !== "function") {
    return () => {};
  }
  statusListeners.add(callback);

  if (engageNativeIpcBridge()) {
    const probe = async () => {
      try {
        const status = await window.apexIPC?.getSidecarStatus?.();
        notifyStatusListeners({
          connected: Boolean(status?.ipcConnected),
          transport: "apex-ipc",
        });
      } catch {
        notifyStatusListeners({ connected: false, transport: "apex-ipc" });
      }
    };
    probe();
    return () => statusListeners.delete(callback);
  }

  notifyStatusListeners({ connected: false, transport: "http-poll" });
  return () => statusListeners.delete(callback);
}

/** Push connection state from the active transport layer (IPC or poll). */
export function publishStreamStatus(connected, transport) {
  notifyStatusListeners({ connected: Boolean(connected), transport });
}

export async function getApexVersion() {
  if (isApexDesktopShell() && window.apexIPC?.getVersion) {
    return window.apexIPC.getVersion();
  }
  return { apex: "30.0.0", stable_base: "30.0.0", transport: "http-poll" };
}

export function getTransportLabel() {
  return isApexDesktopShell() ? "apex-ipc" : "http-poll";
}
