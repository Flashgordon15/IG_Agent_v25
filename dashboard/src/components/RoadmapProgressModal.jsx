import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  Clock,
  Cpu,
  Layers,
  Shield,
  Target,
  TrendingUp,
  X,
  Zap,
} from "lucide-react";
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

/** Locked v29.1 execution blueprint — mirrors docs/architecture/trading_pipeline.md */
const V291_GATE_SEQUENCE = [
  "session_open",
  "session_blackout",
  "cold_start_gap",
  "environment_fitness",
  "points_state",
  "correlation_ok",
  "risk_validation",
  "expectancy_ok",
  "calendar_ok",
  "signal_confidence",
  "ml_veto",
  "execution",
];

const V291_TTL_CACHES = [
  "Daily loss gate — 2s process-wide TTL (daily_loss_policy)",
  "Setup registry — 30s hot-path registry cache",
  "Snapshot slow enrich — 60s dashboard merge TTL",
  "Edge analysis API — 60s response cache",
  "Shadow analytics metrics — 60s rollup cache",
  "Demo readiness — 300s readiness snapshot cache",
];

const V291_BACKGROUND_WORKERS = [
  {
    name: "OrderConfirmWorker",
    role: "POST /positions/otc + confirm poll off tick thread",
    shield: "try/except Exception → release_allocation + audit",
  },
  {
    name: "AlertDispatcherWorker",
    role: "Telegram critical alerts via bounded queue",
    shield: "try/except Exception per job — never blocks gates",
  },
  {
    name: "PointsPersistWorker",
    role: "5s flush of points_state.json off hot path",
    shield: "try/except Exception per engine flush cycle",
  },
];

function V291InMemoryExecutionLayer() {
  const lazyRoutes = [
    "main.py — lazy Gate1 imports; AgentRuntime defers heavy wiring",
    "server_deferred.py — API router mounted post-bind (sub-second :8080)",
    "boot/gate*_runner.py — sequential Gatekeeper pipeline off event loop",
    "post_ready_services.py — AlertDispatcher + background workers after READY",
  ];
  const ttlCaches = [
    "daily_loss_policy — 2s RLock TTL gate cache",
    "setup_registry — 30s registry read cache",
    "snapshot_store — 60s slow-enrich merge TTL",
    "edge_analysis — 60s API response cache",
    "shadow_analytics — 60s metrics rollup cache",
    "demo_readiness — 300s readiness snapshot cache",
  ];
  const workerIsolation = [
    "OrderConfirmWorker — deep-copied params; SUBMITTED returns before IG REST",
    "Failure path — try/except Exception → release_allocation() + audit bus",
    "AlertDispatcherWorker — bounded queue; per-job exception shield",
    "PointsPersistWorker — 5s flush cycle; per-engine try/except on disk I/O",
  ];

  return (
    <section className="rounded-lg border border-warning/35 bg-gradient-to-br from-warning/10 via-bg to-card/30 p-3">
      <div className="mb-3 flex items-start gap-2">
        <Zap className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
        <div>
          <h3 className="text-[12px] font-bold uppercase tracking-wide text-warning">
            v29.1 Low-Latency In-Memory Execution Layer
          </h3>
          <p className="mt-1 text-[10px] leading-relaxed text-muted">
            Thread-safe TTL memory caches · lazy module routing · non-blocking broker
            confirm isolation. Technical log — {APP_VERSION_LABEL} production stack.
          </p>
        </div>
      </div>

      <div className="mb-2 rounded-md border border-border/70 bg-bg/50 p-2.5">
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-foreground">
          Lazy-loaded module import routing
        </p>
        <ul className="space-y-1 text-[10px] text-muted">
          {lazyRoutes.map((line) => (
            <li key={line} className="font-mono leading-snug text-foreground/90">
              {line}
            </li>
          ))}
        </ul>
      </div>

      <div className="mb-2 rounded-md border border-border/70 bg-bg/50 p-2.5">
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-foreground">
          Thread-safe TTL database memory caches
        </p>
        <ul className="space-y-1 text-[10px] text-muted">
          {ttlCaches.map((line) => (
            <li key={line} className="leading-snug">
              <span className="text-warning">▸</span> {line}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded-md border border-accent/25 bg-accent/5 p-2.5">
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-accent">
          OrderConfirmWorker transaction isolation
        </p>
        <ul className="space-y-1 text-[10px] text-muted">
          {workerIsolation.map((line) => (
            <li key={line} className="leading-snug">
              {line}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-[9px] leading-snug text-muted">
          Tick thread never blocks on POST /positions/otc; portfolio gate reservations
          roll back on worker failure without stopping per-epic polling loops.
        </p>
      </div>
    </section>
  );
}

function V291ArchitectureCanvas() {
  return (
    <section className="rounded-lg border border-accent/35 bg-gradient-to-br from-accent/10 via-bg to-card/40 p-3 shadow-inner">
      <div className="mb-3 flex items-start gap-2">
        <Layers className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden />
        <div>
          <h3 className="text-[12px] font-bold uppercase tracking-wide text-accent">
            v29.1 Core Execution &amp; Low-Latency Architecture Canvas
          </h3>
          <p className="mt-1 text-[10px] leading-relaxed text-muted">
            Hub-pull per-epic loops · single-pipeline Gatekeeper boot · thread-safe
            TTL caches · async broker confirm leg. Production locked {APP_VERSION_LABEL}.
          </p>
        </div>
      </div>

      <div className="mb-3 rounded-md border border-border/80 bg-bg/60 p-2.5">
        <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-foreground">
          <Cpu className="h-3 w-3 text-accent" aria-hidden />
          12-Gate Hub-Pull Execution Model
        </div>
        <p className="mb-2 text-[10px] leading-snug text-muted">
          Lightstreamer / REST poll writes to{" "}
          <span className="font-mono text-foreground">MarketDataHub</span>; each{" "}
          <span className="font-mono text-foreground">TradingLoop</span> thread pulls
          quotes every ~5s. Gates run in strict sequence — no epic thread blocks
          another on broker I/O.
        </p>
        <ol className="grid grid-cols-2 gap-1 sm:grid-cols-3">
          {V291_GATE_SEQUENCE.map((gate, idx) => (
            <li
              key={gate}
              className="flex items-center gap-1 rounded border border-border/60 bg-card/50 px-1.5 py-1 text-[9px] font-mono text-foreground"
            >
              <span className="shrink-0 text-accent">{idx + 1}.</span>
              <span className="truncate">{gate}</span>
            </li>
          ))}
        </ol>
      </div>

      <div className="mb-3 grid gap-2 sm:grid-cols-2">
        <div className="rounded-md border border-success/25 bg-success/5 p-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-success">
            <Shield className="h-3 w-3" aria-hidden />
            Single-Pipeline Gatekeeper
          </div>
          <ul className="space-y-1 text-[10px] leading-snug text-muted">
            <li>
              <span className="text-foreground">G1→G5</span> sequential boot via{" "}
              <span className="font-mono">BootCoordinator</span>
            </li>
            <li>Gate N+1 blocked until Gate N reports COMPLETE</li>
            <li>
              Uvicorn binds early;{" "}
              <span className="font-mono">SystemState.READY</span> is sole truth
            </li>
            <li>Lazy router mount — one <span className="font-mono">create_app()</span> per process</li>
          </ul>
        </div>
        <div className="rounded-md border border-warning/25 bg-warning/5 p-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-warning">
            <Zap className="h-3 w-3" aria-hidden />
            Thread-Safe TTL Memory Caches
          </div>
          <ul className="space-y-1 text-[10px] leading-snug text-muted">
            {V291_TTL_CACHES.map((line) => (
              <li key={line} className="flex items-start gap-1">
                <span className="mt-0.5 text-warning">•</span>
                <span>{line}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="rounded-md border border-accent/30 bg-accent/5 p-2.5">
        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-accent">
          Asynchronous OrderConfirmWorker Routing Leg
        </div>
        <p className="mb-2 text-[10px] leading-snug text-muted">
          Validated signals return{" "}
          <span className="font-mono text-foreground">SUBMITTED</span> immediately;
          daemon worker owns IG REST POST + confirm polling. Gate-time portfolio
          reservation released on worker failure via{" "}
          <span className="font-mono">release_allocation()</span>.
        </p>
        <div className="overflow-x-auto rounded border border-border/60">
          <table className="min-w-full text-left text-[9px]">
            <thead className="bg-card/80 text-muted">
              <tr>
                <th className="px-2 py-1 font-semibold">Worker</th>
                <th className="px-2 py-1 font-semibold">Role</th>
                <th className="px-2 py-1 font-semibold">Exception shield</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50 text-foreground">
              {V291_BACKGROUND_WORKERS.map((row) => (
                <tr key={row.name} className="bg-bg/40">
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono text-accent">
                    {row.name}
                  </td>
                  <td className="px-2 py-1.5 text-muted">{row.role}</td>
                  <td className="px-2 py-1.5 text-muted">{row.shield}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
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

          <V291ArchitectureCanvas />

          <V291InMemoryExecutionLayer />

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
