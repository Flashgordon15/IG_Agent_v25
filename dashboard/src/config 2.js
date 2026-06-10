const DEFAULT_ORIGIN = "http://localhost:8080"

function appOrigin() {
  if (typeof window !== "undefined" && window.location?.origin) {
    return window.location.origin
  }
  return DEFAULT_ORIGIN
}

export const API_BASE = appOrigin()

const wsOrigin = API_BASE.startsWith("https://")
  ? API_BASE.replace(/^https:/, "wss:")
  : API_BASE.replace(/^http:/, "ws:")

export const WS_URL = `${wsOrigin}/ws/stream`
