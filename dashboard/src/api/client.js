const API = "";
const AUTH_TOKEN_KEY = "ig_agent_auth_token";
const AUTH_FLAG_KEY = "ig_agent_authenticated";

export function authHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  if (typeof window !== "undefined") {
    const token = window.sessionStorage.getItem(AUTH_TOKEN_KEY);
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  }
  return headers;
}

export function isAuthenticated() {
  if (typeof window === "undefined") return false;
  return window.sessionStorage.getItem(AUTH_FLAG_KEY) === "1";
}

export function clearAuthSession() {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(AUTH_TOKEN_KEY);
  window.sessionStorage.removeItem(AUTH_FLAG_KEY);
}

export function storeAuthSession(token) {
  if (typeof window === "undefined") return;
  if (token) {
    window.sessionStorage.setItem(AUTH_TOKEN_KEY, token);
  }
  window.sessionStorage.setItem(AUTH_FLAG_KEY, "1");
}

export async function fetchJson(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: "include",
    headers: authHeaders(options.headers),
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    let message = text || res.statusText;
    try {
      const body = JSON.parse(text);
      if (body?.detail) {
        message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      /* use raw text */
    }
    if (res.status === 405 && path.includes("/api/system/e2e")) {
      message =
        "E2E endpoint not available — quit and restart IG Agent v25 (old process still running).";
    }
    throw new Error(message);
  }
  return res.json();
}

export const api = {
  state: () => fetchJson("/state"),
  splash: () => fetchJson("/api/splash"),
  dismissSplash: () => fetchJson("/api/splash/dismiss", { method: "POST" }),
  login: async (password) => {
    const res = await fetch(`${API}/api/auth/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data?.detail || "Access Denied");
    }
    const token = res.headers.get("X-Auth-Token");
    storeAuthSession(token);
    return data;
  },
  getStartupStatus: () => fetchJson("/api/startup/status"),
  trades: () => fetchJson("/api/trades?limit=10"),
  signals: () => fetchJson("/api/signals"),
  system: () => fetchJson("/api/system"),
  start: () => fetchJson("/api/start", { method: "POST" }),
  stop: () => fetchJson("/api/stop", { method: "POST" }),
  emergencyStop: () => fetchJson("/api/emergency_stop", { method: "POST" }),
  closeDeal: (dealId) =>
    fetchJson(`/api/close/${encodeURIComponent(dealId)}`, { method: "POST" }),
  runTests: () => fetchJson("/api/system/tests", { method: "POST" }),
  replaySummary: () => fetchJson("/api/replay/summary"),
  shadowToday: () => fetchJson("/api/shadow/today"),
  learningStatus: () => fetchJson("/api/learning/status"),
  replayRun: () => fetchJson("/api/replay/run", { method: "POST" }),
  runE2eCheck: () => fetchJson("/api/system/e2e", { method: "POST" }),
  sentinelDiagnostics: (limit = 60) =>
    fetchJson(`/api/v27/sentinel/diagnostics?limit=${limit}`),
  roadmapProgress: (days = 7) =>
    fetchJson(`/api/roadmap/progress?days=${days}`),
  dailyDigest: () => fetchJson("/api/daily-digest"),
  approveStrategyProposal: (proposalId) =>
    fetchJson("/api/v27/sentinel/approve", {
      method: "POST",
      body: JSON.stringify({ proposal_id: proposalId }),
    }),
  adminForceClose: (epic) =>
    fetchJson("/api/admin/force-close", {
      method: "POST",
      body: JSON.stringify({ epic }),
    }),
  adminForceBreakeven: (epic) =>
    fetchJson("/api/admin/force-breakeven", {
      method: "POST",
      body: JSON.stringify({ epic }),
    }),
  adminRiskStatus: () => fetchJson("/api/admin/risk-status"),
  getHealth: () => fetchJson("/api/health"),
};
