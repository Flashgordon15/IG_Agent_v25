/**
 * Apex IPC tick parser — runs in Electron UtilityProcess (off main thread).
 * Parses newline-delimited JSON from the Unix socket bridge; posts typed events.
 */

/** @param {string} line */
function parseLine(line) {
  const tickString = String(line || "").trim();
  if (!tickString) return { channel: "skip" };
  try {
    const payload = JSON.parse(tickString);
    if (!payload || typeof payload !== "object") {
      return { channel: "tick", raw: tickString, payload: null };
    }
    const type = String(payload.type || "").toLowerCase();
    if (type === "warmup") return { channel: "warmup", payload };
    if (type === "ledger") return { channel: "ledger", payload };
    if (type === "story") return { channel: "story", payload };
    return { channel: "tick", raw: tickString, payload };
  } catch {
    return { channel: "tick", raw: tickString, payload: null };
  }
}

process.parentPort.on("message", (event) => {
  const data = event.data;
  if (!data || typeof data !== "object") return;
  if (data.type === "flush") {
    process.parentPort.postMessage({ channel: "flush" });
    return;
  }
  if (data.type === "lines" && Array.isArray(data.lines)) {
    for (const line of data.lines) {
      const msg = parseLine(line);
      if (msg.channel !== "skip") {
        process.parentPort.postMessage(msg);
      }
    }
    return;
  }
  if (data.type === "line") {
    const msg = parseLine(data.line);
    if (msg.channel !== "skip") {
      process.parentPort.postMessage(msg);
    }
  }
});

process.parentPort.postMessage({ channel: "ready" });
