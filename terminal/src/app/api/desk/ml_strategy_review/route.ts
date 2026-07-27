/**
 * Read-only Next proxy for latest ml_strategy_review_*.json under reports/.
 * Sober status for Quantum Terminal — never loads trading agent PID.
 */

import { NextResponse } from "next/server";
import { promises as fs } from "fs";
import path from "path";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function reportDirCandidates(): string[] {
  const repoRoot = path.resolve(process.cwd(), "..");
  const fromEnv = (process.env.IG_DATA_ROOT || "").trim();
  const out: string[] = [];
  if (fromEnv) {
    out.push(path.resolve(fromEnv, "reports"));
    out.push(path.resolve(repoRoot, fromEnv, "reports"));
  }
  out.push(
    path.join(repoRoot, "src/data/v31-production/reports"),
    path.join(process.cwd(), "src/data/v31-production/reports"),
  );
  return [...new Set(out)];
}

async function findLatestReview(): Promise<{
  path: string;
  data: Record<string, unknown>;
} | null> {
  for (const dir of reportDirCandidates()) {
    let names: string[] = [];
    try {
      names = await fs.readdir(dir);
    } catch {
      continue;
    }
    const files = names
      .filter((n) => /^ml_strategy_review_\d{4}-\d{2}-\d{2}\.json$/.test(n))
      .sort();
    if (!files.length) continue;
    const full = path.join(dir, files[files.length - 1]);
    try {
      const raw = await fs.readFile(full, "utf8");
      const data = JSON.parse(raw) as Record<string, unknown>;
      return { path: full, data };
    } catch {
      continue;
    }
  }
  return null;
}

export async function GET() {
  try {
    const found = await findLatestReview();
    if (!found) {
      return NextResponse.json({
        ok: true,
        missing: true,
        verdict: null,
        day: null,
        chip: { visible: false, verdict: "", label: "", tone: "neutral" },
      });
    }
    const verdict = String(found.data.verdict || "").toUpperCase();
    const day = found.data.day != null ? String(found.data.day) : null;
    const next = found.data.next_one_step != null ? String(found.data.next_one_step) : "";
    // Sober: always show non-EDGE_OK; EDGE_OK stays quiet (not a green badge party).
    const visible = Boolean(verdict) && verdict !== "EDGE_OK";
    const tone =
      verdict === "APP_BLOCKED" || verdict === "NO_EDGE"
        ? "amber"
        : verdict === "NOT_MEASURABLE" || verdict === "EDGE_WEAK"
          ? "neutral"
          : "neutral";
    return NextResponse.json({
      ok: true,
      missing: false,
      verdict,
      day,
      next_one_step: next,
      generated_at: found.data.generated_at ?? null,
      source: found.path,
      chip: {
        visible,
        verdict,
        label: day ? `ML REVIEW ${verdict} · ${day}` : `ML REVIEW ${verdict}`,
        summary: next.slice(0, 160),
        tone,
      },
    });
  } catch (e) {
    return NextResponse.json(
      {
        ok: false,
        missing: true,
        verdict: null,
        chip: { visible: false, verdict: "", label: "", tone: "neutral" },
        error: e instanceof Error ? e.message : "ml_strategy_review_read_failed",
      },
      { status: 200 },
    );
  }
}
