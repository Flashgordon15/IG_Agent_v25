import { API_BASE } from "./config.js";

async function getJson(path, options = {}) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { "Content-Type": "application/json", ...options.headers },
      ...options,
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/** Full tick snapshot (same schema as WebSocket) — not the flat /api/state summary. */
export function fetchState() {
  return getJson("/state");
}

export function fetchTrades() {
  return getJson("/api/trades");
}

export function fetchPoints() {
  return getJson("/api/points");
}

export function fetchReplaySummary() {
  return getJson("/api/replay/summary");
}

export function fetchShadowToday() {
  return getJson("/api/shadow/today");
}

export function fetchLearningStatus() {
  return getJson("/api/learning/status");
}

export async function triggerReplay() {
  try {
    const res = await fetch(`${API_BASE}/api/replay/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return {
        ok: false,
        status: res.status,
        error: data?.error || res.statusText || "Request failed",
      };
    }
    return { ok: true, status: res.status, ...data };
  } catch (err) {
    return { ok: false, error: err?.message || "Network error" };
  }
}

export async function postEmergencyStop() {
  try {
    const res = await fetch(`${API_BASE}/api/emergency_stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "STOP" }),
    });
    if (!res.ok) return { ok: false };
    const data = await res.json().catch(() => ({}));
    return { ok: Boolean(data?.ok ?? true), data };
  } catch {
    return { ok: false };
  }
}
