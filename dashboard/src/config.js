/** v30 Apex desktop monolith — unified API port :9090 (shell + sidecar parity). */
export const APP_VERSION = "30.0.0";
export const APP_VERSION_LABEL = "v30.0";
export const DEFAULT_API_PORT = 9090;
/** Legacy alias — desktop container never probes launchd :8080. */
export const PRODUCTION_API_PORT = 9090;

/**
 * Active API port from Electron bridge or unified shadow default.
 */
export function resolveTargetPort() {
  if (typeof window !== "undefined") {
    const bridged = window.apexConfig?.apiPort;
    if (bridged != null && String(bridged).trim() !== "") {
      const n = Number(bridged);
      if (Number.isFinite(n) && n > 0) return n;
    }
    if (window.apexConfig?.apiBase) {
      try {
        const parsed = new URL(window.apexConfig.apiBase);
        if (parsed.port) {
          const n = Number(parsed.port);
          if (Number.isFinite(n) && n > 0) return n;
        }
      } catch {
        /* use default */
      }
    }
    if (window.location?.protocol === "file:") {
      return DEFAULT_API_PORT;
    }
    if (window.location?.port) {
      const n = Number(window.location.port);
      if (Number.isFinite(n) && n > 0) return n;
    }
  }
  return DEFAULT_API_PORT;
}

export function isShadowProfile() {
  if (typeof window === "undefined") return true;
  if (window.apexConfig?.protectProductionPorts) return true;
  return resolveTargetPort() === DEFAULT_API_PORT;
}

/** Stage 2 / recovery health probe — always :9090 inside the Apex desktop shell. */
export function recoveryHealthUrl() {
  return `http://127.0.0.1:${resolveTargetPort()}/api/health`;
}

function resolveApiBase() {
  if (typeof window !== "undefined" && window.apexConfig?.apiBase) {
    return window.apexConfig.apiBase;
  }
  const port = resolveTargetPort();
  return `http://127.0.0.1:${port}`;
}

export const API_BASE =
  typeof window !== "undefined"
    ? window.apexConfig?.apiBase || resolveApiBase()
    : `http://127.0.0.1:${DEFAULT_API_PORT}`;
