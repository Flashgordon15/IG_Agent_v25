/**
 * Next.js proxy for the performance journal CSV.
 * Reads from the v31 metrics path in the Next process — never loads the
 * trading agent PID with filesystem work.
 *
 * Returns AccountID / ProductType / EngineOrigin so the sovereign blotter can
 * label closes as real CFD/SB tickets without waiting on an agent restart.
 */

import { NextResponse } from "next/server";
import { promises as fs } from "fs";
import path from "path";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export type JournalRow = {
  timestamp: string;
  dealId: string;
  direction: string;
  entry: number | null;
  exit: number | null;
  realizedGbp: number | null;
  closingFillRate: number | null;
  accountId: string;
  productType: string;
  engineOrigin: string;
};

function parseNum(v: string | undefined): number | null {
  if (v == null || String(v).trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function parseCsv(text: string): JournalRow[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return [];
  const header = lines[0].split(",").map((h) => h.trim());
  const idx = (name: string) => header.indexOf(name);
  const iTs = idx("Timestamp");
  const iDeal = idx("DealID");
  const iDir = idx("Direction");
  const iEntry = idx("EntryPrice");
  const iExit = idx("ExitPrice");
  const iPnl = idx("RealizedPnL_GBP");
  const iFill = idx("ClosingFillRate");
  const iAcct = idx("AccountID");
  const iProd = idx("ProductType");
  const iEng = idx("EngineOrigin");

  const rows: JournalRow[] = [];
  for (let i = 1; i < lines.length; i++) {
    // Simple CSV split — journal fields do not embed commas
    const cols = lines[i].split(",");
    const dealId = (cols[iDeal >= 0 ? iDeal : 1] || "").trim();
    if (!dealId || dealId.startsWith("BENCHMARK_OFFSET") || dealId.startsWith("FLAT_SESSION")) {
      continue;
    }
    rows.push({
      timestamp: (cols[iTs >= 0 ? iTs : 0] || "").trim(),
      dealId,
      direction: (cols[iDir >= 0 ? iDir : 2] || "").trim().toUpperCase(),
      entry: parseNum(cols[iEntry >= 0 ? iEntry : 3]),
      exit: parseNum(cols[iExit >= 0 ? iExit : 4]),
      realizedGbp: parseNum(cols[iPnl >= 0 ? iPnl : 5]),
      closingFillRate: parseNum(cols[iFill >= 0 ? iFill : 6]),
      accountId: (cols[iAcct >= 0 ? iAcct : 8] || "").trim(),
      productType: (cols[iProd >= 0 ? iProd : 9] || "").trim().toUpperCase(),
      engineOrigin: (cols[iEng >= 0 ? iEng : 10] || "").trim(),
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
