import { useCallback, useEffect, useState } from "react";
import { Newspaper, X } from "lucide-react";
import { api } from "../api/client.js";
import { renderDigestMarkdown } from "../utils/renderDigestMarkdown.jsx";

const SEEN_KEY = "ig_agent_daily_digest_seen";

export function digestSeenDay() {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(SEEN_KEY);
}

export function markDigestSeen(day) {
  if (!day || typeof window === "undefined") return;
  window.localStorage.setItem(SEEN_KEY, String(day));
}

export function isDigestUnread(day) {
  if (!day) return false;
  return digestSeenDay() !== String(day);
}

export default function DailyDigestModal({ open, onClose, autoOpened = false }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await api.dailyDigest();
      setData(payload);
    } catch (e) {
      setError(e?.message || "Failed to load daily report");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const handleClose = () => {
    if (data?.day) markDigestSeen(data.day);
    onClose?.();
  };

  if (!open) return null;

  const generated = data?.generated_at
    ? new Date(data.generated_at).toLocaleString("en-GB", {
        weekday: "short",
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";

  return (
    <div
      className="fixed inset-0 z-[9997] flex items-end justify-center bg-black/70 backdrop-blur-sm sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-labelledby="daily-digest-title"
      onClick={(e) => e.target === e.currentTarget && handleClose()}
    >
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-t-xl border border-border bg-bg shadow-2xl sm:rounded-xl">
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <Newspaper className="h-4 w-4 text-accent" aria-hidden />
            <div>
              <h2 id="daily-digest-title" className="text-sm font-bold text-foreground">
                Daily Operator Report
              </h2>
              <p className="text-[10px] text-muted">
                {data?.day ? `${data.day}` : "Loading…"} · updated {generated}
                {autoOpened ? " · first open today" : ""}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleClose}
            className="rounded-md border border-border p-1.5 text-muted hover:bg-card"
            aria-label="Close daily report"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="overflow-y-auto px-4 py-3">
          {loading && !data && (
            <p className="py-8 text-center text-sm text-muted">Loading today&apos;s briefing…</p>
          )}
          {error && (
            <p className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[12px] text-danger">
              {error}
            </p>
          )}
          {data?.markdown && (
            <article className="digest-markdown space-y-1">{renderDigestMarkdown(data.markdown)}</article>
          )}
        </div>

        <footer className="flex shrink-0 justify-end gap-2 border-t border-border px-4 py-2">
          <button
            type="button"
            onClick={load}
            disabled={loading}
            className="rounded-md border border-border px-3 py-1.5 text-[11px] text-muted hover:bg-card disabled:opacity-50"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <button
            type="button"
            onClick={handleClose}
            className="rounded-md border border-accent/50 bg-accent/10 px-3 py-1.5 text-[11px] font-semibold text-accent"
          >
            Close
          </button>
        </footer>
      </div>
    </div>
  );
}

export function DailyDigestButton({ onClick, unread = false }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "relative inline-flex items-center justify-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-semibold transition-colors",
        unread
          ? "border-accent bg-accent/20 text-accent animate-pulse hover:bg-accent/30"
          : "border-border bg-card/60 text-foreground hover:bg-card",
      ].join(" ")}
      title="Today's operator briefing — updated each morning"
    >
      <Newspaper className="h-3.5 w-3.5" aria-hidden />
      Daily Report
      {unread ? (
        <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-accent" aria-hidden />
      ) : null}
    </button>
  );
}

export async function fetchDigestDay() {
  try {
    const payload = await api.dailyDigest();
    return payload?.day ?? null;
  } catch {
    return null;
  }
}
