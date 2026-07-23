/** v32 dual-engine REST bases — CFD :8080 · SB :8081 */

export const CFD_API_DEFAULT = "http://127.0.0.1:8080";
export const SB_API_DEFAULT = "http://127.0.0.1:8081";

export type DeskEnginePort = "cfd" | "sb";

export function cfdHttpBase(): string {
  const explicit = process.env.NEXT_PUBLIC_CFD_API?.replace(/\/$/, "");
  if (explicit) return explicit;
  const legacy = process.env.NEXT_PUBLIC_AGENT_URL?.replace(/\/$/, "");
  if (legacy) return legacy;
  if (typeof window !== "undefined") {
    if (window.location.port === "3000" || window.location.port === "3001") {
      return CFD_API_DEFAULT;
    }
    return window.location.origin;
  }
  return CFD_API_DEFAULT;
}

export function sbHttpBase(): string {
  const explicit = process.env.NEXT_PUBLIC_SB_API?.replace(/\/$/, "");
  if (explicit) return explicit;
  return SB_API_DEFAULT;
}

export function deskHttpBase(port: DeskEnginePort): string {
  return port === "cfd" ? cfdHttpBase() : sbHttpBase();
}

export function deskWsBase(port: DeskEnginePort = "cfd"): string {
  return deskHttpBase(port).replace(/^http/i, "ws");
}

export async function fetchDeskJson<T>(
  base: string,
  path: string,
  init?: RequestInit,
  timeoutMs = 4000,
): Promise<T> {
  const url = `${base.replace(/\/$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
    });
    if (!res.ok) {
      throw new Error(`Desk ${url} HTTP ${res.status}`);
    }
    return res.json() as Promise<T>;
  } catch (e) {
    if (e instanceof Error && e.name === "AbortError") {
      throw new Error(`Desk ${path} timed out after ${timeoutMs}ms`);
    }
    throw e;
  }
}
