import { NextResponse } from "next/server";

const LOG_PATH =
  process.env.IG_AGENT_ENGINE_LOG ??
  "/Users/chrisgordon/Projects/IG-Agent-v31-Sandbox/src/data/logs/engine.log";

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
