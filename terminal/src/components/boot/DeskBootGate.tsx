"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { DeskBootSplash } from "@/components/boot/DeskBootSplash";

type Props = {
  children: React.ReactNode;
};

/** Minimum splash visibility so native handoff never flashes blank desk. */
const MIN_SPLASH_MS = 1800;

/**
 * Blocks the main Trading Desk until dual-desk harness clears for view
 * (ready_for_desk or ready_for_view / SoT-safe twin). Entry-armed remains
 * stricter and is shown on the splash — not required to open the desk UI.
 */
export function DeskBootGate({ children }: Props) {
  const [ready, setReady] = useState(false);
  const [minElapsed, setMinElapsed] = useState(false);
  const gateReadyRef = useRef(false);
  const mountedAt = useRef(Date.now());

  useEffect(() => {
    const id = window.setTimeout(() => setMinElapsed(true), MIN_SPLASH_MS);
    return () => window.clearTimeout(id);
  }, []);

  const onReady = useCallback(() => {
    gateReadyRef.current = true;
    const wait = Math.max(0, MIN_SPLASH_MS - (Date.now() - mountedAt.current));
    window.setTimeout(() => {
      if (gateReadyRef.current) setReady(true);
    }, wait);
  }, []);

  useEffect(() => {
    if (minElapsed && gateReadyRef.current) setReady(true);
  }, [minElapsed]);

  if (!ready) {
    return <DeskBootSplash onReady={onReady} pollMs={1500} />;
  }
  return <>{children}</>;
}
