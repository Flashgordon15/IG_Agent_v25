import type { JsonObject } from "../types/cockpit";
import type { WsConnectionState } from "../types/cockpit";

export type WsStatusHandler = (state: WsConnectionState, detail?: string) => void;
export type TickHandler = (tick: JsonObject) => void;

const HEARTBEAT_DEGRADED_MS = 15_000;
const HEARTBEAT_DEAD_MS = 30_000;
const MAX_BACKOFF_MS = 30_000;
const BASE_BACKOFF_MS = 1_000;

/**
 * Browser WebSocket with exponential backoff + heartbeat detection.
 * Non-blocking — all callbacks are sync-safe; batch ticks externally.
 */
export class ResilientWebSocket {
  private ws: WebSocket | null = null;
  private backoffMs = BASE_BACKOFF_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lastMessageAt = 0;
  private disposed = false;
  private state: WsConnectionState = "disconnected";

  constructor(
    private readonly url: string,
    private readonly onTick: TickHandler,
    private readonly onStatus: WsStatusHandler,
  ) {}

  connect(): void {
    if (this.disposed) return;
    this.clearReconnect();
    this.setState("reconnecting");

    try {
      this.ws = new WebSocket(this.url);
    } catch (err) {
      this.scheduleReconnect(String(err));
      return;
    }

    this.ws.onopen = () => {
      this.backoffMs = BASE_BACKOFF_MS;
      this.lastMessageAt = Date.now();
      this.setState("connected");
      this.startHeartbeat();
    };

    this.ws.onmessage = (event) => {
      this.lastMessageAt = Date.now();
      if (this.state === "degraded") this.setState("connected");
      try {
        this.onTick(JSON.parse(event.data as string) as JsonObject);
      } catch {
        /* ignore malformed */
      }
    };

    this.ws.onerror = () => {
      /* onclose handles reconnect */
    };

    this.ws.onclose = () => {
      this.stopHeartbeat();
      if (!this.disposed) this.scheduleReconnect("connection closed");
    };
  }

  dispose(): void {
    this.disposed = true;
    this.clearReconnect();
    this.stopHeartbeat();
    this.ws?.close();
    this.ws = null;
    this.setState("disconnected");
  }

  private setState(next: WsConnectionState, detail?: string): void {
    if (this.state === next && !detail) return;
    this.state = next;
    this.onStatus(next, detail);
  }

  private scheduleReconnect(reason: string): void {
    if (this.disposed) return;
    this.ws = null;
    this.setState("reconnecting", reason);
    this.clearReconnect();
    this.reconnectTimer = setTimeout(() => {
      this.backoffMs = Math.min(this.backoffMs * 2, MAX_BACKOFF_MS);
      this.connect();
    }, this.backoffMs);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (!this.lastMessageAt) return;
      const silent = Date.now() - this.lastMessageAt;
      if (silent >= HEARTBEAT_DEAD_MS) {
        this.setState("disconnected", "heartbeat timeout");
        this.ws?.close();
      } else if (silent >= HEARTBEAT_DEGRADED_MS) {
        this.setState("degraded", "no ticks");
      }
    }, 3_000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }
}
