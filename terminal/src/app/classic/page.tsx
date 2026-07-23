import { ErrorBoundary } from "@/components/ErrorBoundary";
import { TerminalShell } from "@/components/TerminalShell";

/** Legacy Adaptive Logistics ops view — not the primary Trading Desk product path. */
export default function ClassicDeskPage() {
  return (
    <ErrorBoundary label="Adaptive Logistics">
      <TerminalShell />
    </ErrorBoundary>
  );
}
