import { NextResponse } from "next/server";
import path from "node:path";

const DEFAULT_LOG = path.join(
  process.cwd(),
  "..",
  "src",
  "data",
  "v31-production",
  "logs",
  "engine.log",
);

const LOG_PATH = process.env.IG_AGENT_ENGINE_LOG ?? DEFAULT_LOG;
const TAIL_BYTES = 96 * 1024;

export async function GET() {
  try {
    const fh = await import("node:fs/promises").then((m) => m.open(LOG_PATH, "r"));
    const stat = await fh.stat();
    const start = Math.max(0, stat.size - TAIL_BYTES);
    const buf = Buffer.alloc(stat.size - start);
    await fh.read(buf, 0, buf.length, start);
    await fh.close();
    return NextResponse.json({
      ok: true,
      log_tail: buf.toString("utf8"),
      path: LOG_PATH,
    });
  } catch (err) {
    return NextResponse.json(
      {
        ok: false,
        log_tail: "",
        error: err instanceof Error ? err.message : "engine log unreadable",
      },
      { status: 200 },
    );
  }
}
