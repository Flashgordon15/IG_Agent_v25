import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Clock, Target, X, TrendingUp } from "lucide-react";
import { api } from "../api/client.js";
import { APP_VERSION_LABEL } from "../utils/roadmapTelemetry.js";

/** Verified production capabilities — shipped in v29.1 core. */
const ROADMAP_COMPLETE = [
  "Sub-microsecond Trailing Stop Evaluation Engine (~1.5µs execution)",
  "Asynchronous Non-Blocking Broker Stop Dispatch Worker",
  "Real-time 3-Stage Boot Progress Bar & Password Firewall",
  "IG 0.5% Commercial FX Fee & Automated Min-Distance Clamps",
  "2-Per-Epic Allocation Cap Loops Priority Harmonization",
];

/** Quantitative upgrades deferred until post-soak baseline (STRATEGIC_ENHANCEMENTS_ROADMAP). */
const ROADMAP_PLANNED = [
  "Dynamic Spread-to-ATR News Spike Protection (Target: 20% limit)",
  "Asymmetric Time-Based Stale Position Decay Exits",
  "Correlation Density Confidence Floor Risk Scaler",
];

function PctBar({ pct, className = "" }) {
  const n = Math.max(0, Math.min(100, Number(pct) || 0));
  const bar =
    n >= 70 ? "bg-success" : n >= 40 ? "bg-warning" : "bg-accent";
  return (
    <div className={["flex items-center gap-2", className].join(" ")}>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-border">
        <div
          className={`h-full rounded-full transition-all duration-500 ${bar}`}
          style={{ width: `${n}%` }}
        />
      </div>
      <span className="w-9 shrink-0 text-right font-mono text-[11px] tabular-nums text-foreground">
        {n}%
      </span>
    </div>
  );
}

function FeatureMatrixSection({ title, subtitle, items, variant }) {
  const isComplete = variant === "complete";
  return (
    <section
      className={[
        "rounded-lg border p-3",
        isComplete
          ? "border-success/30 bg-success/5"
          : "border-warning/30 bg-warning/5",
      ].join(" ")}
    >
      <div className="mb-2">
        <h3
          className={[
            "text-[12px] font-semibold uppercase tracking-wide",
            isComplete ? "text-success" : "text-warning",
          ].join(" ")}
        >
          {title}
        </h3>
        {subtitle ? (
          <p className="mt-0.5 text-[10px] leading-snug text-muted">{subtitle}</p>
        ) : null}
      </div>
      <ul className="space-y-2">
        {items.map((label) => (
          <li
            key={label}
            className={[
              "flex items-start gap-2 rounded-md border px-2.5 py-2",
              isComplete
                ? "border-success/25 bg-success/10"
                : "border-warning/25 bg-warning/10",
            ].join(" ")}
          >
            {isComplete ? (
              <CheckCircle2
                className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success"
                aria-hidden
              />
            ) : (
              <Clock
                className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning"
                aria-hidden
              />
            )}
            <span className="text-[11px] leading-snug text-foreground">
              {isComplete ? `✓ ${label}` : `⏳ ${label}`}
            </span>
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
      setError(e?.message || "Failed to load roadmap telemetry");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  if (!open) return null;

  const overall = data?.overall_pct ?? 0;
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
              <h2
                id="roadmap-progress-title"
                className="text-sm font-bold text-foreground"
              >
                Strategic Enhancements Roadmap
              </h2>
              <p className="text-[10px] text-muted">
                {APP_VERSION_LABEL} · production vs post-soak quantitative goals
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

        <div className="space-y-3 overflow-y-auto px-4 py-3">
          <FeatureMatrixSection
            title="Production complete"
            subtitle="Verified capabilities deployed in the live v29.1 core platform."
            items={ROADMAP_COMPLETE}
            variant="complete"
          />

          <FeatureMatrixSection
            title="Planned / future soak goals"
            subtitle="Implement only after the v29.1 live soak establishes a stable performance baseline."
            items={ROADMAP_PLANNED}
            variant="planned"
          />

          {error ? (
            <p className="rounded-md border border-border bg-card/50 px-3 py-2 text-[11px] text-muted">
              Live telemetry unavailable — feature matrix above is authoritative.
            </p>
          ) : null}

          {data ? (
            <section className="rounded-lg border border-border bg-surface/40 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">
                  Soak telemetry snapshot
                </span>
                <span className="flex items-center gap-1 text-[10px] text-muted">
                  <TrendingUp className="h-3.5 w-3.5" aria-hidden />
                  £{target}/day target
                </span>
              </div>
              <PctBar pct={overall} />
              <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-muted sm:grid-cols-4">
                <div>
                  14d net:{" "}
                  <span className="font-mono text-foreground">
                    £{Number(data.profitability_14d?.net_gbp ?? 0).toFixed(2)}
                  </span>
                </div>
                <div>
                  WR:{" "}
                  <span className="font-mono text-foreground">
                    {data.profitability_14d?.wr_pct ?? "—"}%
                  </span>
                </div>
                <div>
                  Trade-ready:{" "}
                  <span className="font-mono text-foreground">
                    {data.feeder_today?.trade_ready ?? 0}
                  </span>
                </div>
                <div>
                  Intents:{" "}
                  <span className="font-mono text-foreground">
                    {data.feeder_today?.order_intents ?? 0}
                  </span>
                </div>
              </div>
            </section>
          ) : loading ? (
            <p className="py-2 text-center text-[11px] text-muted">
              Loading soak telemetry…
            </p>
          ) : null}
        </div>

        <footer className="flex shrink-0 justify-end gap-2 border-t border-border px-4 py-2">
          <button
            type="button"
            onClick={load}
            disabled={loading}
            className="rounded-md border border-border px-3 py-1.5 text-[11px] text-muted hover:bg-card disabled:opacity-50"
          >
            {loading ? "Refreshing…" : "Refresh telemetry"}
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
      title="Strategic enhancements roadmap — production vs planned"
    >
      <Target className="h-3.5 w-3.5" aria-hidden />
      Roadmap
    </button>
  );
}
