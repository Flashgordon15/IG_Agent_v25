/**
 * Next.js proxy for settled/cancelled learning-DB closes.
 * Reads SQLite in the Next process only — zero I/O on the trading agent PID.
 */

import { NextResponse } from "next/server";
import { existsSync } from "fs";
import path from "path";
import { DatabaseSync } from "node:sqlite";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type ClosedRow = {
  timestamp: string;
  dealId: string;
  direction: string;
  entry: number | null;
  exit: number | null;
  realizedGbp: number | null;
  market: string;
  epic: string;
  result: string;
  size: number | null;
  pnlPoints: number | null;
  quality: "settled" | "cancelled" | "flat" | "unknown";
};

function resolveDb(): string | null {
  const root = path.resolve(process.cwd(), "..");
  const candidates = [
    path.join(root, "src/data/v31-production/learning_db.sqlite3"),
    path.join(process.cwd(), "src/data/v31-production/learning_db.sqlite3"),
    path.join(root, "src/data/learning_db.sqlite3"),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return null;
}

function qualityOf(
  result: string,
  entry: number | null,
  exit: number | null,
  gbp: number | null,
): ClosedRow["quality"] {
  const r = result.toUpperCase();
  if (r === "CANCELLED" || r === "REJECTED") return "cancelled";
  if (r === "WIN" || r === "LOSS" || r === "BREAKEVEN") return "settled";
  if (
    entry != null &&
    exit != null &&
    Math.abs(entry - exit) < 1e-9 &&
    (gbp == null || Math.abs(gbp) < 1e-9)
  ) {
    return "flat";
  }
  if (gbp != null && Number.isFinite(gbp)) return "settled";
  return "unknown";
}

export async function GET() {
  try {
    const dbPath = resolveDb();
    if (!dbPath) {
      return NextResponse.json({
        ok: true,
        rows: [],
        note: "learning_db_missing",
      });
    }

    const db = new DatabaseSync(dbPath, { readOnly: true });
    const today = new Date().toISOString().slice(0, 10);
    const stmt = db.prepare(`
      SELECT ig_deal_id, side, entry, exit, market, epic, pnl_points, size,
             closed_at, result, unrealized_pnl
      FROM trades
      WHERE closed_at IS NOT NULL
        AND (closed_at LIKE ? OR closed_at LIKE ?)
      ORDER BY closed_at DESC
      LIMIT 120
    `);
    const raw = stmt.all(`${today}%`, `${today}T%`) as Array<{
      ig_deal_id: string | null;
      side: string | null;
      entry: number | null;
      exit: number | null;
      market: string | null;
      epic: string | null;
      pnl_points: number | null;
      size: number | null;
      closed_at: string | null;
      result: string | null;
      unrealized_pnl: number | null;
    }>;
    db.close();

    const rows: ClosedRow[] = raw
      .map((r) => {
        const size = r.size != null ? Number(r.size) : null;
        const pts = r.pnl_points != null ? Number(r.pnl_points) : null;
        const result = String(r.result || "").toUpperCase();
        const entry = r.entry != null ? Number(r.entry) : null;
        const exit = r.exit != null ? Number(r.exit) : null;
        // Only materialize GBP from points×size on non-cancelled settles.
        let gbp: number | null = null;
        const isCancel =
          result === "CANCELLED" ||
          result === "REJECTED" ||
          (entry != null &&
            exit != null &&
            Math.abs(entry - exit) < 1e-9 &&
            (pts == null || Math.abs(pts) < 1e-9));
        if (
          !isCancel &&
          pts != null &&
          size != null &&
          Number.isFinite(pts * size)
        ) {
          gbp = Math.round(pts * size * 100) / 100;
        }
        return {
          timestamp: String(r.closed_at || ""),
          dealId: String(r.ig_deal_id || ""),
          direction: String(r.side || "").toUpperCase(),
          entry,
          exit,
          realizedGbp: gbp,
          market: String(r.market || r.epic || ""),
          epic: String(r.epic || ""),
          result,
          size,
          pnlPoints: pts,
          quality: qualityOf(result, entry, exit, gbp),
        };
      })
      .filter((r) => r.dealId);

    return NextResponse.json({
      ok: true,
      rows,
      count: rows.length,
      session_day: today,
      source: dbPath,
    });
  } catch (e) {
    return NextResponse.json(
      {
        ok: false,
        rows: [],
        error: e instanceof Error ? e.message : "closed_ledger_failed",
      },
      { status: 200 },
    );
  }
}
