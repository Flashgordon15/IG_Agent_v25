import { useCallback, useEffect, useState } from "react";
import {
  fetchLearningStatus,
  fetchReplaySummary,
  fetchShadowToday,
  triggerReplay,
} from "../api.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function Card({ title, children, className = "", loading = false }) {
  return (
    <section
      className={[
        "rounded-lg border border-border bg-card p-3 sm:p-4",
        className,
      ].join(" ")}
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        {title && <h2 className="label-caps">{title}</h2>}
        {loading && (
          <span className="inline-flex items-center gap-1.5 text-[10px] text-muted">
            <span
              className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-muted border-t-accent"
              aria-hidden
            />
            Loading…
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

function StatusBadge({ label, className }) {
  return (
    <span
      className={[
        "inline-flex rounded-full border px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        className,
      ].join(" ")}
    >
      {label}
    </span>
  );
}

function fmtTs(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-GB", {
      dateStyle: "short",
      timeStyle: "medium",
    });
  } catch {
    return String(ts);
  }
}

function fmtPct(value, digits = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtWinRate(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const pct = n <= 1 ? n * 100 : n;
  return `${pct.toFixed(1)}%`;
}

function normalizeProgress(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  if (n > 1) return Math.min(1, n / 100);
  return Math.max(0, Math.min(1, n));
}

function normalizeReplay(raw) {
  if (!raw || typeof raw !== "object") return null;

  if (
    raw.last_run_time != null ||
    raw.bars_processed != null ||
    raw.calibration_factor != null
  ) {
    return {
      lastRunTime: raw.last_run_time ?? raw.last_run,
      barsProcessed: raw.bars_processed ?? raw.bars_cache,
      calibrationFactor: raw.calibration_factor,
      status: raw.status ?? "idle",
    };
  }

  const replayState = raw.replay_state || {};
  const lastResult = raw.last_result || {};
  const schedulerTime =
    replayState.last_run_time ??
    replayState.last_run ??
    replayState.last_replay_timestamp;
  return {
    lastRunTime: schedulerTime ?? lastResult.timestamp ?? lastResult.bar_time ?? raw.last_updated,
    barsProcessed:
      replayState.bars_processed ??
      replayState.bar_count ??
      replayState.bars_cache ??
      replayState.results_rows ??
      raw.bars_analysed,
    calibrationFactor:
      replayState.calibration_factor ?? raw.calibration_factor,
    status: replayState.status ?? "idle",
  };
}

function isReplayApiWindowOpen() {
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/London",
      hour: "numeric",
      minute: "numeric",
      hour12: false,
    }).formatToParts(new Date());
    const hour = Number(parts.find((p) => p.type === "hour")?.value ?? 0);
    const minute = Number(parts.find((p) => p.type === "minute")?.value ?? 0);
    const mins = hour * 60 + minute;
    return mins >= 7 * 60 && mins < 22 * 60 + 30;
  } catch {
    return true;
  }
}

function normalizeShadow(raw) {
  if (!raw || typeof raw !== "object") return null;

  const recordCount = raw.record_count ?? raw.evaluations ?? 0;
  let firedRate = raw.would_have_fired_rate;
  if (firedRate == null && recordCount > 0) {
    const would = raw.would_have_traded ?? 0;
    firedRate = would / recordCount;
  }
  firedRate = Number(firedRate);
  if (!Number.isFinite(firedRate)) firedRate = 0;

  const topSetups = (raw.top_3_setups || []).map((row) => ({
    name: row.setup ?? row.setup_key ?? "unknown",
    count: row.count,
  }));

  return { recordCount, firedRate, topSetups };
}

function normalizeLearning(raw) {
  if (!raw || typeof raw !== "object") return null;

  const mlRows = raw.ml_store_rows ?? raw.ml_records ?? 0;
  const confirmed = raw.confirmed_trade_count ?? raw.confirmed_trades ?? 0;
  const progress = normalizeProgress(
    raw.progress_to_500 ?? raw.ml_viability_pct ?? confirmed / 500,
  );

  const topSetups = (raw.top_setups_by_win_rate || raw.top_setups || []).map(
    (row) => ({
      setup: row.setup_key ?? row.setup ?? "—",
      trades: row.count ?? row.trades ?? 0,
      winRate: row.win_rate,
    }),
  );

  return { mlRows, confirmed, progress, topSetups };
}

function calibrationMeta(factor) {
  const n = Number(factor);
  const pct = Number.isFinite(n) ? n * 100 : 0;
  if (pct >= 90) {
    return {
      pct,
      badge: "GOOD",
      className: "border-success/40 bg-success/10 text-success",
    };
  }
  if (pct >= 70) {
    return {
      pct,
      badge: "ACCEPTABLE",
      className: "border-warning/40 bg-warning/10 text-warning",
    };
  }
  return {
    pct,
    badge: "REVIEW NEEDED",
    className: "border-danger/40 bg-danger/10 text-danger",
  };
}

function signalQualityMeta(rate) {
  const n = Number(rate);
  if (!Number.isFinite(n) || n < 0.2) {
    return {
      badge: "Low signal quality today",
      className: "border-danger/40 bg-danger/10 text-danger",
    };
  }
  if (n <= 0.4) {
    return {
      badge: "ACCEPTABLE",
      className: "border-warning/40 bg-warning/10 text-warning",
    };
  }
  return {
    badge: "GOOD",
    className: "border-success/40 bg-success/10 text-success",
  };
}

function hasReplayRun(replay) {
  if (replay?.status === "running") return true;
  const t = replay?.lastRunTime;
  if (t == null || t === "") return false;
  if (typeof t === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(t)) {
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// IntelligencePanel
// ---------------------------------------------------------------------------

export default function IntelligencePanel({ state: _state }) {
  const [replay, setReplay] = useState(null);
  const [shadow, setShadow] = useState(null);
  const [learning, setLearning] = useState(null);

  const [replayLoading, setReplayLoading] = useState(true);
  const [shadowLoading, setShadowLoading] = useState(true);
  const [learningLoading, setLearningLoading] = useState(true);

  const [replayError, setReplayError] = useState(null);
  const [shadowError, setShadowError] = useState(null);
  const [learningError, setLearningError] = useState(null);
  const [replayRunning, setReplayRunning] = useState(false);
  const [replayRunError, setReplayRunError] = useState(null);
  const replayWindowOpen = isReplayApiWindowOpen();

  const loadAll = useCallback(async () => {
    setReplayLoading(true);
    setShadowLoading(true);
    setLearningLoading(true);

    const [replayRaw, shadowRaw, learningRaw] = await Promise.all([
      fetchReplaySummary(),
      fetchShadowToday(),
      fetchLearningStatus(),
    ]);

    if (replayRaw == null) {
      setReplayError("Failed to load replay summary");
    } else {
      setReplay(normalizeReplay(replayRaw));
      setReplayError(null);
    }
    setReplayLoading(false);

    if (shadowRaw == null) {
      setShadowError("Failed to load shadow log");
    } else {
      setShadow(normalizeShadow(shadowRaw));
      setShadowError(null);
    }
    setShadowLoading(false);

    if (learningRaw == null) {
      setLearningError("Failed to load learning status");
    } else {
      setLearning(normalizeLearning(learningRaw));
      setLearningError(null);
    }
    setLearningLoading(false);
  }, []);

  useEffect(() => {
    loadAll();
    const id = window.setInterval(loadAll, 30000);
    return () => window.clearInterval(id);
  }, [loadAll]);

  useEffect(() => {
    if (!replayRunning) return undefined;
    const id = window.setInterval(loadAll, 5000);
    return () => window.clearInterval(id);
  }, [replayRunning, loadAll]);

  useEffect(() => {
    if (replay?.status === "running") {
      setReplayRunning(true);
    } else if (replay?.status === "idle" || replay?.status === "failed") {
      setReplayRunning(false);
    }
  }, [replay?.status]);

  const handleRunReplay = async () => {
    setReplayRunError(null);
    setReplayRunning(true);
    const result = await triggerReplay();
    if (!result?.ok) {
      setReplayRunning(false);
      setReplayRunError(
        result?.error ||
          (result?.status === 409
            ? "Outside 07:00–22:30 London"
            : "Failed to start replay"),
      );
      return;
    }
    await loadAll();
  };

  const replayMeta =
    replay?.calibrationFactor != null
      ? calibrationMeta(replay.calibrationFactor)
      : null;
  const shadowMeta = shadow ? signalQualityMeta(shadow.firedRate) : null;

  return (
    <div className="mx-auto max-w-5xl space-y-3 px-1 pb-4">
      <Card title="Replay summary" loading={replayLoading}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={handleRunReplay}
            disabled={!replayWindowOpen || replayRunning}
            title={
              replayWindowOpen
                ? "Run fetch + replay + analysis now"
                : "Replay only runs 07:00–22:30 London (outside live quiet hours)"
            }
            className={[
              "rounded border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide transition-colors",
              !replayWindowOpen || replayRunning
                ? "cursor-not-allowed border-border bg-bg text-muted opacity-60"
                : "border-accent bg-accent/15 text-accent hover:bg-accent/25",
            ].join(" ")}
          >
            {replayRunning ? "Running…" : "Run replay"}
          </button>
          {replayRunning && (
            <span className="text-[11px] text-muted">Pipeline in progress…</span>
          )}
        </div>
        {replayRunError && (
          <p className="mb-2 text-[12px] text-danger">{replayRunError}</p>
        )}
        {replayError ? (
          <p className="py-2 text-[12px] text-danger">{replayError}</p>
        ) : replayLoading && !replay ? (
          <p className="py-4 text-center text-[12px] text-muted">Loading…</p>
        ) : !hasReplayRun(replay) ? (
          <p className="py-4 text-center text-[12px] text-muted">
            No replay run yet
          </p>
        ) : (
          <div className="space-y-3">
            <dl className="grid grid-cols-1 gap-2 text-[12px] sm:grid-cols-3 sm:text-[13px]">
              <div>
                <dt className="text-muted">Last run</dt>
                <dd className="mt-0.5 tabular-nums text-foreground">
                  {fmtTs(replay.lastRunTime)}
                </dd>
              </div>
              <div>
                <dt className="text-muted">Bars processed</dt>
                <dd className="mt-0.5 font-mono tabular-nums text-foreground">
                  {replay.barsProcessed ?? "—"}
                </dd>
              </div>
              <div>
                <dt className="text-muted">Calibration</dt>
                <dd className="mt-0.5 font-mono tabular-nums text-foreground">
                  {replayMeta ? `${replayMeta.pct.toFixed(1)}%` : "—"}
                </dd>
              </div>
            </dl>
            {replayMeta && (
              <div className="flex justify-center pt-1">
                <StatusBadge
                  label={replayMeta.badge}
                  className={replayMeta.className}
                />
              </div>
            )}
          </div>
        )}
      </Card>

      <Card title="Signal quality (shadow)" loading={shadowLoading}>
        {shadowError ? (
          <p className="py-2 text-[12px] text-danger">{shadowError}</p>
        ) : shadowLoading && !shadow ? (
          <p className="py-4 text-center text-[12px] text-muted">Loading…</p>
        ) : (
          <div className="space-y-3">
            <dl className="grid grid-cols-1 gap-2 text-[12px] sm:grid-cols-2 sm:text-[13px]">
              <div>
                <dt className="text-muted">Records today</dt>
                <dd className="mt-0.5 font-mono tabular-nums text-foreground">
                  {shadow.recordCount}
                </dd>
              </div>
              <div>
                <dt className="text-muted">Would-have-fired rate</dt>
                <dd className="mt-0.5 font-mono tabular-nums text-foreground">
                  {fmtPct(shadow.firedRate)}
                </dd>
              </div>
            </dl>

            {shadowMeta && (
              <div className="flex justify-center">
                <StatusBadge
                  label={shadowMeta.badge}
                  className={shadowMeta.className}
                />
              </div>
            )}

            <div>
              <p className="mb-1.5 text-[10px] uppercase tracking-wide text-muted">
                Top setups
              </p>
              {shadow.topSetups.length === 0 ? (
                <p className="text-[12px] text-muted">No fired setups today</p>
              ) : (
                <ul className="space-y-1 text-[12px]">
                  {shadow.topSetups.map((row) => (
                    <li
                      key={row.name}
                      className="flex items-center justify-between rounded border border-border/60 bg-bg/40 px-2 py-1"
                    >
                      <span className="font-mono text-foreground">{row.name}</span>
                      {row.count != null && (
                        <span className="tabular-nums text-muted">
                          {row.count}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </Card>

      <Card title="Learning status" loading={learningLoading}>
        {learningError ? (
          <p className="py-2 text-[12px] text-danger">{learningError}</p>
        ) : learningLoading && !learning ? (
          <p className="py-4 text-center text-[12px] text-muted">Loading…</p>
        ) : (
          <div className="space-y-3">
            <dl className="grid grid-cols-1 gap-2 text-[12px] sm:grid-cols-2 sm:text-[13px]">
              <div>
                <dt className="text-muted">ML store rows</dt>
                <dd className="mt-0.5 font-mono tabular-nums text-foreground">
                  {learning.mlRows}
                </dd>
              </div>
              <div>
                <dt className="text-muted">Confirmed trades</dt>
                <dd className="mt-0.5 font-mono tabular-nums text-foreground">
                  {learning.confirmed}
                </dd>
              </div>
            </dl>

            <div>
              <div className="h-2 overflow-hidden rounded bg-border">
                <div
                  className="h-full bg-accent transition-all duration-300"
                  style={{ width: `${learning.progress * 100}%` }}
                />
              </div>
              <p className="mt-1.5 text-[11px] tabular-nums text-muted">
                {learning.confirmed} / 500 trades to full model confidence
              </p>
            </div>

            {learning.mlRows < 10 ? (
              <p className="text-center text-[12px] text-warning">
                Model not yet trained
              </p>
            ) : (
              <div className="-mx-1 overflow-x-auto">
                <table className="w-full min-w-[280px] text-left text-[11px] sm:text-[12px]">
                  <thead>
                    <tr className="border-b border-border text-muted">
                      <th className="px-2 py-1.5 font-normal">Setup</th>
                      <th className="px-2 py-1.5 font-normal">Trades</th>
                      <th className="px-2 py-1.5 font-normal">Win Rate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {learning.topSetups.length === 0 ? (
                      <tr>
                        <td
                          colSpan={3}
                          className="px-2 py-3 text-center text-muted"
                        >
                          No setup stats yet
                        </td>
                      </tr>
                    ) : (
                      learning.topSetups.map((row) => (
                        <tr
                          key={row.setup}
                          className="border-b border-border/60 last:border-0"
                        >
                          <td className="px-2 py-2 font-mono text-foreground">
                            {row.setup}
                          </td>
                          <td className="px-2 py-2 tabular-nums">
                            {row.trades}
                          </td>
                          <td className="px-2 py-2 tabular-nums">
                            {fmtWinRate(row.winRate)}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
