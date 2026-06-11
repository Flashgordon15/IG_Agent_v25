import { useCallback, useEffect, useState } from "react";
import { Target, X, TrendingUp, ChevronRight } from "lucide-react";
import { api } from "../api/client.js";

function statusColor(status) {
  const s = String(status ?? "").toUpperCase();
  if (s === "PASS") return "text-success";
  if (s === "WARN" || s === "WARNING") return "text-warning";
  if (s === "FAIL" || s === "STOP") return "text-danger";
  return "text-muted";
}

function statusBg(status) {
  const s = String(status ?? "").toUpperCase();
  if (s === "PASS") return "bg-success/15 border-success/30";
  if (s === "WARN" || s === "WARNING") return "bg-warning/15 border-warning/30";
  if (s === "FAIL" || s === "STOP") return "bg-danger/15 border-danger/30";
  return "bg-card border-border";
}

function PctBar({ pct, className = "" }) {
  const n = Math.max(0, Math.min(100, Number(pct) || 0));
  const bar =
    n >= 70 ? "bg-success" : n >= 40 ? "bg-warning" : "bg-accent";
  return (
    <div className={["flex items-center gap-2", className].join(" ")}>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-border">
        <div className={`h-full rounded-full transition-all duration-500 ${bar}`} style={{ width: `${n}%` }} />
      </div>
      <span className="w-9 shrink-0 text-right font-mono text-[11px] tabular-nums text-foreground">{n}%</span>
    </div>
  );
}

function HistorySparkline({ history, sectionId }) {
  if (!Array.isArray(history) || history.length < 2) return null;
  const points = history
    .map((h) => {
      const sec = (h.sections || []).find((s) => s.id === sectionId);
      return sec?.pct;
    })
    .filter((v) => v != null);
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const delta = points[points.length - 1] - points[0];
  return (
    <span className={`text-[10px] tabular-nums ${delta >= 0 ? "text-success" : "text-danger"}`}>
      {delta >= 0 ? "+" : ""}{delta}% vs {history.length}d
    </span>
  );
}

function SectionBlock({ section, history }) {
  return (
    <section className="rounded-lg border border-border bg-surface/40 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[12px] font-semibold uppercase tracking-wide text-foreground">{section.title}</h3>
        <div className="flex items-center gap-2">
          <HistorySparkline history={history} sectionId={section.id} />
          <span className="font-mono text-[11px] tabular-nums text-muted">{section.pct}%</span>
        </div>
      </div>
      <PctBar pct={section.pct} className="mb-3" />
      <ul className="space-y-2">
        {(section.items || []).map((item) => (
          <li
            key={item.id}
            className={["rounded-md border px-2.5 py-2", statusBg(item.status)].join(" ")}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <p className="text-[11px] font-semibold text-foreground">{item.label}</p>
                <p className="mt-0.5 text-[10px] leading-snug text-muted">{item.detail}</p>
                {item.action && (
                  <p className="mt-1 flex items-start gap-1 text-[10px] leading-snug text-accent/90">
                    <ChevronRight className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                    {item.action}
                  </p>
                )}
              </div>
              <span className={`shrink-0 text-[10px] font-bold uppercase ${statusColor(item.status)}`}>
                {item.status}
              </span>
            </div>
            <PctBar pct={item.pct} className="mt-2" />
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function RoadmapProgressModal({ open, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await api.roadmapProgress(14);
      setData(payload);
    } catch (e) {
      setError(e?.message || "Failed to load roadmap");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  if (!open) return null;

  const overall = data?.overall_pct ?? 0;
  const milestone = data?.milestone ?? "—";
  const target = data?.target_daily_gbp ?? 1000;

  return (
    <div
      className="fixed inset-0 z-[9998] flex items-end justify-center bg-black/70 backdrop-blur-sm sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-labelledby="roadmap-progress-title"
      onClick={(e) => e.target === e.currentTarget && onClose?.()}
    >
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-t-xl border border-border bg-bg shadow-2xl sm:rounded-xl">
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <Target className="h-4 w-4 text-accent" aria-hidden />
            <div>
              <h2 id="roadmap-progress-title" className="text-sm font-bold text-foreground">
                Roadmap to £{target}/day
              </h2>
              <p className="text-[10px] text-muted">
                Milestone {milestone} · updated {data?.generated_at ? new Date(data.generated_at).toLocaleString("en-GB") : "—"}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border p-1.5 text-muted hover:bg-card"
            aria-label="Close roadmap"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="overflow-y-auto px-4 py-3 space-y-3">
          {loading && !data && (
            <p className="py-8 text-center text-sm text-muted">Loading gap audit…</p>
          )}
          {error && (
            <p className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[12px] text-danger">{error}</p>
          )}
          {data && (
            <>
              <div className="rounded-lg border border-accent/30 bg-accent/5 p-3">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-accent">Overall progress</span>
                  <span className="flex items-center gap-1 text-[11px] text-muted">
                    <TrendingUp className="h-3.5 w-3.5" aria-hidden />
                    Daily snapshot
                  </span>
                </div>
                <PctBar pct={overall} />
                <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-muted sm:grid-cols-4">
                  <div>14d net: <span className="text-foreground font-mono">£{Number(data.profitability_14d?.net_gbp ?? 0).toFixed(2)}</span></div>
                  <div>WR: <span className="text-foreground font-mono">{data.profitability_14d?.wr_pct ?? "—"}%</span></div>
                  <div>Trade-ready: <span className="text-foreground font-mono">{data.feeder_today?.trade_ready ?? 0}</span></div>
                  <div>Intents: <span className="text-foreground font-mono">{data.feeder_today?.order_intents ?? 0}</span></div>
                </div>
              </div>

              {(data.sections || []).map((sec) => (
                <SectionBlock key={sec.id} section={sec} history={data.history} />
              ))}

              {Array.isArray(data.history) && data.history.length > 1 && (
                <section className="rounded-lg border border-border bg-card/50 p-3">
                  <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">7-day trend</h3>
                  <ul className="space-y-1 text-[10px]">
                    {data.history.slice(-7).map((h) => (
                      <li key={h.day} className="flex justify-between tabular-nums text-muted">
                        <span>{h.day}</span>
                        <span className="text-foreground">{h.overall_pct ?? "—"}% overall</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </div>

        <footer className="shrink-0 border-t border-border px-4 py-2 flex justify-end gap-2">
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
            onClick={onClose}
            className="rounded-md border border-accent/50 bg-accent/10 px-3 py-1.5 text-[11px] font-semibold text-accent"
          >
            Close
          </button>
        </footer>
      </div>
    </div>
  );
}

export function RoadmapProgressButton({ onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center justify-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] font-semibold text-accent transition-colors hover:bg-accent/20"
      title="Roadmap progress toward £1,000/day"
    >
      <Target className="h-3.5 w-3.5" aria-hidden />
      Roadmap
    </button>
  );
}
