"use client";

import { DeskBootSplash } from "@/components/boot/DeskBootSplash";

/** Dedicated splash URL for Trading_Desk.app — redirects to / when gate clears. */
export default function BootPage() {
  return <DeskBootSplash redirectOnReady pollMs={1500} />;
}
