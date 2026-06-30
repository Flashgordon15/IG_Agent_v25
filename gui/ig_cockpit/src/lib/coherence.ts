import type { JsonObject } from "../types/cockpit";

export function parsePayloadTs(payload: JsonObject | null | undefined): number {
  if (!payload?.ts) return 0;
  const t = Date.parse(String(payload.ts));
  return Number.isFinite(t) ? t : 0;
}

/** Apply REST payload only if not older than last known WS tick timestamp. */
export function mergeIfNewer(
  current: JsonObject | null,
  incoming: JsonObject,
  wsTickTs: number,
): JsonObject {
  const incomingTs = parsePayloadTs(incoming);
  if (wsTickTs > 0 && incomingTs > 0 && incomingTs < wsTickTs - 500) {
    return current ?? incoming;
  }
  if (current) {
    const currentTs = parsePayloadTs(current);
    if (incomingTs > 0 && currentTs > incomingTs) return current;
  }
  return incoming;
}

export function debounce<T extends (...args: never[]) => void>(
  fn: T,
  ms: number,
): T & { cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const debounced = ((...args: Parameters<T>) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, ms);
  }) as T & { cancel: () => void };
  debounced.cancel = () => {
    if (timer) clearTimeout(timer);
    timer = null;
  };
  return debounced;
}
