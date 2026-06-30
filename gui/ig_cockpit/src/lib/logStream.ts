import type { LogLevel, LogLine, LogSubsystem } from "../types/cockpit";
import type { JsonObject } from "../types/cockpit";

let logSeq = 0;

function nextId(): string {
  logSeq += 1;
  return `log-${logSeq}-${Date.now()}`;
}

function classifySubsystem(message: string): LogSubsystem {
  const m = message.toLowerCase();
  if (m.includes("feed") || m.includes("stream") || m.includes("quote")) return "feeds";
  if (m.includes("route") || m.includes("path_") || m.includes("execution_path")) return "routing";
  if (m.includes("siz") || m.includes("stake")) return "sizing";
  if (m.includes("govern") || m.includes("enforce") || m.includes("regime")) return "governance";
  if (m.includes("order") || m.includes("fill") || m.includes("pipeline")) return "execution";
  return "execution";
}

export function logFromTick(tick: JsonObject, prev?: JsonObject | null): LogLine[] {
  const lines: LogLine[] = [];
  const ts = String(tick.ts ?? new Date().toISOString());

  const signal = tick.signal as JsonObject | undefined;
  const direction = String(signal?.direction ?? "");
  const prevDir = String((prev?.signal as JsonObject | undefined)?.direction ?? "");

  if (direction && direction !== prevDir && direction !== "WAIT") {
    lines.push({
      id: nextId(),
      ts,
      message: `Signal ${direction} conf=${String(signal?.confidence ?? "?")}`,
      level: "info",
      subsystem: "execution",
    });
  }

  const errors = tick.errors as JsonObject | undefined;
  const errCount = Number(errors?.count ?? 0);
  const prevErr = Number((prev?.errors as JsonObject | undefined)?.count ?? 0);
  if (errCount > prevErr) {
    lines.push({
      id: nextId(),
      ts,
      message: `Engine error count ${prevErr} → ${errCount} (${String(errors?.type ?? "unknown")})`,
      level: "error",
      subsystem: "execution",
    });
  }

  if (tick.trading_paused === true && prev?.trading_paused !== true) {
    lines.push({
      id: nextId(),
      ts,
      message: "Trading paused via API",
      level: "warn",
      subsystem: "governance",
    });
  }

  const health = tick.health as JsonObject | undefined;
  const summary = String(health?.summary ?? "");
  const prevSummary = String((prev?.health as JsonObject | undefined)?.summary ?? "");
  if (summary && summary !== prevSummary) {
    lines.push({
      id: nextId(),
      ts,
      message: summary,
      level: summary.includes("fail") ? "warn" : "info",
      subsystem: classifySubsystem(summary),
    });
  }

  return lines;
}

export function mergeLogLines(existing: LogLine[], incoming: LogLine[], max = 500): LogLine[] {
  if (incoming.length === 0) return existing;
  const merged = [...existing, ...incoming];
  return merged.slice(-max);
}

export function filterLogs(lines: LogLine[], subsystem: LogSubsystem): LogLine[] {
  if (subsystem === "all") return lines;
  return lines.filter((l) => l.subsystem === subsystem);
}

export function levelColor(level: LogLevel): string {
  switch (level) {
    case "error":
      return "text-danger";
    case "warn":
      return "text-warning";
    default:
      return "text-text-secondary";
  }
}
