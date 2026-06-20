/** Normalize fetch failures to ambient weekend backfill copy. */
export const MOCK_BACKFILL_MSG =
  "MOCK BACKFILL CACHE: ACTIVE · STANDING BY FOR SUNDAY BELL";

export function normalizeHudError(message) {
  const raw = String(message || "").trim();
  if (!raw) return MOCK_BACKFILL_MSG;
  const lower = raw.toLowerCase();
  if (
    lower.includes("failed to fetch") ||
    lower.includes("network") ||
    lower.includes("unavailable") ||
    lower.includes("http 5")
  ) {
    return MOCK_BACKFILL_MSG;
  }
  return raw;
}
