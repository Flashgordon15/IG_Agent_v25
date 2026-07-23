import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DeskBootGate } from "@/components/boot/DeskBootGate";
import { GpuPlatformShell } from "@/components/gpu/GpuPlatformShell";

/** Canonical Trading Desk product route — sovereign P&L + open/closed blotters. */
export default function DeskPage() {
  return (
    <ErrorBoundary label="Trading Desk">
      <DeskBootGate>
        <GpuPlatformShell />
      </DeskBootGate>
    </ErrorBoundary>
  );
}
