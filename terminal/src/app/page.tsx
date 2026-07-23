import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DeskBootGate } from "@/components/boot/DeskBootGate";
import { GpuPlatformShell } from "@/components/gpu/GpuPlatformShell";

export default function Home() {
  return (
    <ErrorBoundary label="AI Sniper Command Deck">
      <DeskBootGate>
        <GpuPlatformShell />
      </DeskBootGate>
    </ErrorBoundary>
  );
}
