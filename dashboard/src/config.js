/** v30 Apex desktop monolith — unified API port :9090 (shell + sidecar parity). */
export const APP_VERSION = "30.0.0";
export const APP_VERSION_LABEL = "v30.0";
export const DEFAULT_API_PORT = 9090;
/** Legacy alias — desktop container never probes launchd :8080. */
export const PRODUCTION_API_PORT = 9090;
/** Live Vanguard bind port (parallel track supervisor). */
export const LIVE_VANGUARD_PORT = 8080;
/** Shadow simulator bind port (v30 parallel track). */
export const SHADOW_SIMULATOR_PORT = 9199;

export function isLiveVanguardPort() {
  return resolveTargetPort() === LIVE_VANGUARD_PORT;
}

export function isShadowSimulatorPort() {
  return resolveTargetPort() === SHADOW_SIMULATOR_PORT;
}

/** Single-process unified engine dashboard (:8080). */
export function isUnifiedEnginePort() {
  return resolveTargetPort() === LIVE_VANGUARD_PORT;
}

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
  const port = resolveTargetPort();
  return port === DEFAULT_API_PORT || port === SHADOW_SIMULATOR_PORT;
}

/** Same-origin API base — avoids localhost vs 127.0.0.1 CORS when tunneled. */
export function resolveApiBase() {
  if (typeof window !== "undefined" && window.apexConfig?.apiBase) {
    return window.apexConfig.apiBase;
  }
  if (typeof window !== "undefined" && window.location?.protocol?.startsWith("http")) {
    const pagePort = Number(window.location.port || "0");
    const targetPort = resolveTargetPort();
    if (pagePort > 0 && pagePort === targetPort) {
      return window.location.origin;
    }
    const host = window.location.hostname || "127.0.0.1";
    return `http://${host}:${targetPort}`;
  }
  const port = resolveTargetPort();
  return `http://127.0.0.1:${port}`;
}

/** Stage 2 / recovery health probe — same host as the loaded dashboard page. */
export function recoveryHealthUrl() {
  return `${resolveApiBase()}/api/health`;
}

export const API_BASE =
  typeof window !== "undefined"
    ? resolveApiBase()
    : `http://127.0.0.1:${DEFAULT_API_PORT}`;
