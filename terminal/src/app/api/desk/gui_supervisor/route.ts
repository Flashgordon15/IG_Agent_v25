/**
 * Read-only Next proxy for GUI desk supervisor SoT JSON.
 * Quantum Terminal polls this — never loads the trading agent PID.
 */

import { NextResponse } from "next/server";
import { promises as fs } from "fs";
import path from "path";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type SupervisorPayload = {
  score?: string;
  needs_code?: boolean;
  needs_ops?: boolean;
  checked_at?: string;
  top_finding?: {
    title?: string;
    detail?: string;
    severity?: string;
    class?: string;
  } | null;
  dashboard_chip?: {
    visible?: boolean;
    score?: string;
    label?: string;
    summary?: string;
    tone?: string;
    state_path?: string;
    alerts?: string[];
    needs_code?: boolean;
    needs_ops?: boolean;
  } | null;
  alerts?: string[];
  halted?: boolean;
  cursor_handoff?: unknown;
  findings?: Array<{ severity?: string; title?: string }>;
};

function supervisorStateCandidates(): string[] {
  const repoRoot = path.resolve(process.cwd(), "..");
  const fileName = "gui_supervisor_latest.json";
  const fromEnv = (process.env.IG_DATA_ROOT || "").trim();
  const out: string[] = [];
  if (fromEnv) {
    // Absolute IG_DATA_ROOT, or relative to repo / cwd.
    out.push(path.resolve(fromEnv, "state", fileName));
    out.push(path.resolve(repoRoot, fromEnv, "state", fileName));
    out.push(path.resolve(process.cwd(), fromEnv, "state", fileName));
  }
  out.push(
    path.join(repoRoot, "src/data/v31-production/state", fileName),
    path.join(process.cwd(), "src/data/v31-production/state", fileName),
    path.join(repoRoot, "src/data/state", fileName),
  );
  return [...new Set(out)];
}

export async function GET() {
  try {
    const candidates = supervisorStateCandidates();
    let raw = "";
    let used = "";
    for (const p of candidates) {
      try {
        raw = await fs.readFile(p, "utf8");
        used = p;
        break;
      } catch {
        /* try next */
      }
    }
    if (!raw) {
      return NextResponse.json({
        ok: true,
        missing: true,
        score: "UNKNOWN",
        chip: { visible: false, score: "UNKNOWN", label: "", summary: "" },
        source: null,
      });
    }
    const data = JSON.parse(raw) as SupervisorPayload;
    const chipIn = data.dashboard_chip;
    const halted = Boolean(data.halted) || String(chipIn?.score || "").toUpperCase() === "HALTED";
    const score = halted
      ? "HALTED"
      : String(chipIn?.score || data.score || "UNKNOWN").toUpperCase();
    const top = data.top_finding;
    const alerts = Array.from(
      new Set(
        [
          ...((chipIn && chipIn.alerts) || []),
          ...(data.alerts || []),
          ...(halted ? ["HALTED", "BLEED"] : []),
        ]
          .map((a) => String(a || "").trim().toUpperCase())
          .filter(Boolean),
      ),
    );
    const summary =
      (chipIn && chipIn.summary) ||
      (top && top.title) ||
      (Array.isArray(data.findings)
        ? data.findings.find((f) => f.severity === "fail" || f.severity === "watch")
            ?.title
        : null) ||
      "";
    const visible =
      chipIn?.visible != null
        ? Boolean(chipIn.visible)
        : score === "WATCH" || score === "FAIL" || score === "HALTED" || halted;
    const tone =
      chipIn?.tone ||
      (score === "FAIL" || score === "HALTED" || halted
        ? "red"
        : score === "WATCH"
          ? "amber"
          : "green");
    const label =
      chipIn?.label ||
      (halted || score === "HALTED"
        ? "SUPERVISOR HALTED · BLEED LOCK"
        : `SUPERVISOR ${score}${alerts.length ? ` · ${alerts.join(" · ")}` : ""}`);
    return NextResponse.json({
      ok: true,
      missing: false,
      score,
      halted,
      alerts,
      needs_code: Boolean(data.needs_code),
      needs_ops: Boolean(data.needs_ops),
      checked_at: data.checked_at ?? null,
      top_finding: top ?? null,
      cursor_handoff: data.cursor_handoff ?? null,
      chip: {
        visible,
        score,
        label,
        summary,
        tone,
        alerts,
        state_path:
          chipIn?.state_path ||
          "src/data/v31-production/state/gui_supervisor_latest.json",
        needs_code: Boolean(data.needs_code),
        needs_ops: Boolean(data.needs_ops),
      },
      source: used,
    });
  } catch (e) {
    return NextResponse.json(
      {
        ok: false,
        missing: true,
        score: "UNKNOWN",
        chip: { visible: false, score: "UNKNOWN", label: "", summary: "" },
        error: e instanceof Error ? e.message : "gui_supervisor_read_failed",
      },
      { status: 200 },
    );
  }
}
