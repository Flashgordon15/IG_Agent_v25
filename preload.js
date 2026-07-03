/**
 * Apex IPC preload — zero-copy Unix socket bridge (Phase 4).
 * Renderer receives read-only handles; no Node integration in page context.
 */

const { contextBridge, ipcRenderer } = require("electron");

function readRuntimeFromMain() {
  try {
    const cfg = ipcRenderer.sendSync("apex:runtime-config-sync");
    if (cfg && typeof cfg === "object") return cfg;
  } catch (err) {
    console.error("apex:runtime-config-sync failed", err);
  }
  return {
    profile: "live",
    apiPort: 8080,
    apiBase: "http://127.0.0.1:8080",
    cockpitPort: 9191,
    cockpitBase: "http://127.0.0.1:9191",
    protectProductionPorts: true,
    v30Only: true,
    shellBackground: "#0a0e14",
    version: "30.0.0",
    versionLabel: "v30.0",
  };
}

const RUNTIME = Object.freeze(readRuntimeFromMain());

const bootPhaseListeners = new Set();
const tickListeners = new Set();
const statusListeners = new Set();
const warmupListeners = new Set();
const storyListeners = new Set();
const ledgerListeners = new Set();

function normalizeTickPayload(tickPayload) {
  if (typeof tickPayload === "string") {
    try {
      return JSON.parse(tickPayload);
    } catch (err) {
      console.error("apex:tick JSON parse error", err);
      return null;
    }
  }
  return tickPayload;
}

ipcRenderer.on("apex:boot-phase", (_event, payload) => {
  for (const cb of bootPhaseListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apex:boot-phase listener error", err);
    }
  }
});

ipcRenderer.on("apex:tick", (_event, tickPayload) => {
  const payload = normalizeTickPayload(tickPayload);
  if (payload == null) return;
  for (const cb of tickListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apex:tick listener error", err);
    }
  }
});

ipcRenderer.on("apex:ipc-status", (_event, payload) => {
  for (const cb of statusListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apex:ipc-status listener error", err);
    }
  }
});

ipcRenderer.on("apex:warmup", (_event, payload) => {
  for (const cb of warmupListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apex:warmup listener error", err);
    }
  }
});

ipcRenderer.on("apex:ledger", (_event, payload) => {
  for (const cb of ledgerListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apex:ledger listener error", err);
    }
  }
});

ipcRenderer.on("apex:story", (_event, payload) => {
  for (const cb of storyListeners) {
    try {
      cb(payload);
    } catch (err) {
      console.error("apex:story listener error", err);
    }
  }
});

contextBridge.exposeInMainWorld(
  "apexConfig",
  Object.freeze({
    apiBase: RUNTIME.apiBase,
    apiPort: RUNTIME.apiPort,
    cockpitBase: RUNTIME.cockpitBase,
    cockpitPort: RUNTIME.cockpitPort,
    profile: RUNTIME.profile,
    protectProductionPorts: RUNTIME.protectProductionPorts,
    v30Only: RUNTIME.v30Only,
    shellBackground: RUNTIME.shellBackground,
    version: RUNTIME.version,
    versionLabel: RUNTIME.versionLabel,
  })
);

contextBridge.exposeInMainWorld(
  "apexIPC",
  Object.freeze({
    onTick(callback) {
      if (typeof callback !== "function") return () => {};
      tickListeners.add(callback);
      return () => tickListeners.delete(callback);
    },
    onStatus(callback) {
      if (typeof callback !== "function") return () => {};
      statusListeners.add(callback);
      return () => statusListeners.delete(callback);
    },
    onWarmup(callback) {
      if (typeof callback !== "function") return () => {};
      warmupListeners.add(callback);
      return () => warmupListeners.delete(callback);
    },
    onLedger(callback) {
      if (typeof callback !== "function") return () => {};
      ledgerListeners.add(callback);
      return () => ledgerListeners.delete(callback);
    },
    onStory(callback) {
      if (typeof callback !== "function") return () => {};
      storyListeners.add(callback);
      return () => storyListeners.delete(callback);
    },
    onBootPhase(callback) {
      if (typeof callback !== "function") return () => {};
      bootPhaseListeners.add(callback);
      return () => bootPhaseListeners.delete(callback);
    },
    getVersion() {
      return ipcRenderer.invoke("apex:get-version");
    },
    getSidecarStatus() {
      return ipcRenderer.invoke("apex:sidecar-status");
    },
    restartSidecar() {
      return ipcRenderer.invoke("apex:restart-sidecar");
    },
    exportWarmupReport() {
      return ipcRenderer.invoke("apex:export-warmup-report");
    },
    isDesktopShell() {
      return true;
    },
  })
);
