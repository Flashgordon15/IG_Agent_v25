/**
 * ResilientWebSocket state machine validation — degraded / reconnect / recovery.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResilientWebSocket } from "./wsClient";
import type { WsConnectionState } from "../types/cockpit";

type WsHandler = {
  open?: () => void;
  message?: (data: string) => void;
  close?: () => void;
  error?: () => void;
};

class MockWebSocket {
  static OPEN = 1;
  static instances: MockWebSocket[] = [];
  readyState = MockWebSocket.OPEN;
  private handlers: WsHandler = {};

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }

  set onopen(fn: () => void) {
    this.handlers.open = fn;
    fn();
  }

  set onmessage(fn: (ev: { data: string }) => void) {
    this.handlers.message = (data) => fn({ data });
  }

  set onclose(fn: () => void) {
    this.handlers.close = fn;
  }

  set onerror(fn: () => void) {
    this.handlers.error = fn;
  }

  simulateMessage(data: string): void {
    this.handlers.message?.(data);
  }

  close(): void {
    this.readyState = 3;
    this.handlers.close?.();
  }
}

describe("ResilientWebSocket runtime states", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("transitions reconnecting → connected on open", () => {
    const states: WsConnectionState[] = [];
    const ws = new ResilientWebSocket(
      "ws://127.0.0.1:8080/ws/stream",
      () => undefined,
      (s) => states.push(s),
    );
    ws.connect();
    expect(states).toContain("reconnecting");
    expect(states).toContain("connected");
    ws.dispose();
  });

  it("enters degraded after 15s silence and recovers on tick", () => {
    const states: WsConnectionState[] = [];
    const ws = new ResilientWebSocket(
      "ws://127.0.0.1:8080/ws/stream",
      () => undefined,
      (s) => states.push(s),
    );
    ws.connect();

    vi.advanceTimersByTime(16_000);
    expect(states).toContain("degraded");

    MockWebSocket.instances[0]?.simulateMessage(
      JSON.stringify({ ts: new Date().toISOString(), bid: 1, offer: 2 }),
    );
    expect(states[states.length - 1]).toBe("connected");
    ws.dispose();
  });

  it("schedules reconnect with exponential backoff on close", () => {
    const states: WsConnectionState[] = [];
    const ws = new ResilientWebSocket(
      "ws://127.0.0.1:8080/ws/stream",
      () => undefined,
      (s) => states.push(s),
    );
    ws.connect();
    const countBefore = MockWebSocket.instances.length;
    MockWebSocket.instances[0]?.close();
    expect(states).toContain("reconnecting");

    vi.advanceTimersByTime(1_000);
    expect(MockWebSocket.instances.length).toBeGreaterThan(countBefore);
    ws.dispose();
  });
});
