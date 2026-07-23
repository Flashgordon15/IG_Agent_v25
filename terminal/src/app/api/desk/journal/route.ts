/**
 * Next.js proxy for the performance journal CSV.
 * Reads from the v31 metrics path in the Next process — never loads the
 * trading agent PID with filesystem work.
 */

import { NextResponse } from "next/server";
import { promises as fs } from "fs";
import path from "path";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type JournalRow = {
  timestamp: string;
  dealId: string;
  direction: string;
  entry: number | null;
  exit: number | null;
  realizedGbp: number | null;
  closingFillRate: number | null;
};

function parseNum(v: string | undefined): number | null {
  if (v == null || String(v).trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function parseCsv(text: string): JournalRow[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return [];
  const rows: JournalRow[] = [];
  for (let i = 1; i < lines.length; i++) {
    // Simple CSV split — journal fields do not embed commas
    const cols = lines[i].split(",");
    const dealId = (cols[1] || "").trim();
    if (!dealId || dealId.startsWith("BENCHMARK_OFFSET") || dealId.startsWith("FLAT_SESSION")) {
      continue;
    }
    rows.push({
      timestamp: (cols[0] || "").trim(),
      dealId,
      direction: (cols[2] || "").trim().toUpperCase(),
      entry: parseNum(cols[3]),
      exit: parseNum(cols[4]),
      realizedGbp: parseNum(cols[5]),
      closingFillRate: parseNum(cols[6]),
    });
  }
  return rows;
}

export async function GET() {
  try {
    const root = path.resolve(process.cwd(), "..");
    const candidates = [
      path.join(root, "src/data/v31-production/metrics/daily_journal.csv"),
      path.join(process.cwd(), "src/data/v31-production/metrics/daily_journal.csv"),
      path.join(root, "src/data/metrics/daily_journal.csv"),
    ];
    let text = "";
    let used = "";
    for (const p of candidates) {
      try {
        text = await fs.readFile(p, "utf8");
        used = p;
        break;
      } catch {
        /* try next */
      }
    }
    if (!text) {
      return NextResponse.json({
        ok: true,
        rows: [],
        source: null,
        note: "journal_missing",
      });
    }
    const all = parseCsv(text);
    const today = new Date().toISOString().slice(0, 10);
    const session = all.filter((r) => r.timestamp.startsWith(today));
    const rows = (session.length ? session : all.slice(-48)).reverse();
    return NextResponse.json({
      ok: true,
      rows,
      count: rows.length,
      source: used,
      session_day: today,
    });
  } catch (e) {
    return NextResponse.json(
      {
        ok: false,
        rows: [],
        error: e instanceof Error ? e.message : "journal_read_failed",
      },
      { status: 200 },
    );
  }
}
