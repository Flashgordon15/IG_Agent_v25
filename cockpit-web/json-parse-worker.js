/**
 * Off-main-thread JSON parse for large Flight Deck poll payloads.
 */
self.onmessage = (event) => {
  const id = event.data && event.data.id;
  const text = event.data && event.data.text;
  if (id == null) return;
  try {
    const data = JSON.parse(String(text || ""));
    self.postMessage({
      id,
      ok: true,
      data: data && typeof data === "object" ? data : null,
    });
  } catch (err) {
    self.postMessage({
      id,
      ok: false,
      error: String(err && err.message ? err.message : err),
    });
  }
};
