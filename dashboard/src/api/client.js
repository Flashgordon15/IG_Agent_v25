const API = "";

export async function fetchJson(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

export const api = {
  state: () => fetchJson("/state"),
  splash: () => fetchJson("/api/splash"),
  dismissSplash: () => fetchJson("/api/splash/dismiss", { method: "POST" }),
  trades: () => fetchJson("/api/trades"),
  signals: () => fetchJson("/api/signals"),
  system: () => fetchJson("/api/system"),
  start: () => fetchJson("/api/start", { method: "POST" }),
  stop: () => fetchJson("/api/stop", { method: "POST" }),
  emergencyStop: () => fetchJson("/api/emergency_stop", { method: "POST" }),
  closeDeal: (dealId) =>
    fetchJson(`/api/close/${encodeURIComponent(dealId)}`, { method: "POST" }),
  runTests: () => fetchJson("/api/system/tests", { method: "POST" }),
};
