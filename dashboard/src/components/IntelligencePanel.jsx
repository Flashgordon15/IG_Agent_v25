import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchIntelligenceDashboard,
  triggerReplay,
} from "../api.js";
import { useApexTelemetry } from "../apex/useApexTelemetry.js";
import { ASSET_LABELS, EPIC_TO_ASSET } from "../apex/constants.js";
import { normalizeHudError } from "../utils/hudErrors.js";

const EMPTY_MSG = "Awaiting initial market telemetry streams…";

function GlassCard({ title, tag, children, loading = false }) {
  return (
    <section className="apex-intel-card">
      <header className="apex-intel-card__header">
        <h2>{title}</h2>
        {tag && <span className="apex-intel-card__tag">{tag}</span>}
        {loading && <span className="apex-intel-card__loading">Loading…</span>}
      </header>
      {children}
    </section>
  );
}

function StatusBadge({ label, className }) {
  return (
    <span className={`apex-intel-badge ${className ?? ""}`}>{label}</span>
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
  return `${(n <= 1 ? n * 100 : n).toFixed(digits)}%`;
}

function fmtWinRate(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n <= 1 ? n * 100 : n).toFixed(1)}%`;
}

function normalizeProgress(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  if (n > 1) return Math.min(1, n / 100);
  return Math.max(0, Math.min(1, n));
}

function normalizeReplay(raw) {
  if (!raw || typeof raw !== "object") return null;
  const mtime = raw.last_updated ?? raw.last_run_time;
  return {
    lastRunTime: mtime,
    barsProcessed: raw.bars_analysed ?? raw.bars_processed ?? raw.bars_cache ?? 0,
    calibrationFactor: raw.calibration_factor,
    status: raw.status ?? "idle",
    recommendations: Array.isArray(raw.recommendations) ? raw.recommendations : [],
  };
}

function normalizeShadow(raw) {
  if (!raw || typeof raw !== "object") return null;
  const recordCount = raw.evaluations ?? raw.record_count ?? 0;
  let firedRate = raw.would_have_fired_rate;
  if (firedRate == null && recordCount > 0) {
    firedRate = (raw.would_have_traded ?? 0) / recordCount;
  }
  if (!Number.isFinite(Number(firedRate))) firedRate = 0;
  const topSetups = (raw.top_3_setups || []).map((row) => ({
    name: row.setup ?? row.setup_key ?? "unknown",
    count: row.count,
  }));
  return { recordCount, firedRate, topSetups, topBlocked: raw.top_blocked_setup };
}

function normalizeLearning(raw) {
  if (!raw || typeof raw !== "object") return null;
  const mlRows = raw.ml_records ?? raw.ml_store_rows ?? 0;
  const confirmed = raw.confirmed_trades ?? raw.confirmed_trade_count ?? 0;
  const target = raw.ml_viability_target ?? 500;
  const progress = normalizeProgress(
    raw.ml_viability_pct != null ? raw.ml_viability_pct / 100 : confirmed / target,
  );
  const topSetups = (raw.top_setups || raw.top_setups_by_win_rate || []).map((row) => ({
    setup: row.setup_key ?? row.setup ?? "—",
    trades: row.count ?? row.trades ?? 0,
    winRate: row.win_rate,
  }));
  return { mlRows, confirmed, progress, target, topSetups };
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

function calibrationMeta(factor) {
  const n = Number(factor);
  const pct = Number.isFinite(n) ? n * 100 : 0;
  if (pct >= 90) return { pct, badge: "GOOD", className: "apex-intel-badge--ok" };
  if (pct >= 70) return { pct, badge: "ACCEPTABLE", className: "apex-intel-badge--warn" };
  return { pct, badge: "REVIEW NEEDED", className: "apex-intel-badge--fail" };
}

function signalQualityMeta(rate) {
  const n = Number(rate);
  if (!Number.isFinite(n) || n < 0.2) {
    return { badge: "LOW SIGNAL QUALITY TODAY", className: "apex-intel-badge--crimson" };
  }
  if (n <= 0.4) return { badge: "ACCEPTABLE", className: "apex-intel-badge--warn" };
  return { badge: "GOOD", className: "apex-intel-badge--ok" };
}

function epicLabel(epic) {
  const asset = EPIC_TO_ASSET[epic];
  return asset ? ASSET_LABELS[asset] ?? epic : epic;
}

export default function IntelligencePanel() {
  const { telemetry } = useApexTelemetry();
  const [dashboard, setDashboard] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [replayRunning, setReplayRunning] = useState(false);
  const [replayRunError, setReplayRunError] = useState(null);
  const replayWindowOpen = isReplayApiWindowOpen();

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const payload = await fetchIntelligenceDashboard();
    if (!payload) {
      setError(normalizeHudError("Intelligence dashboard unavailable — sidecar may be warming."));
      setDashboard(null);
    } else {
      setDashboard(payload);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, 30000);
    return () => window.clearInterval(id);
  }, [load]);

  const replay = useMemo(
    () => normalizeReplay(dashboard?.replay),
    [dashboard?.replay],
  );
  const shadow = useMemo(
    () => normalizeShadow(dashboard?.shadow),
    [dashboard?.shadow],
  );
  const learning = useMemo(
    () => normalizeLearning(dashboard?.learning),
    [dashboard?.learning],
  );
  const funnel = dashboard?.funnel ?? telemetry?.transparency?.funnel ?? null;
  const microBackend = dashboard?.microstructure ?? {};
  const mlHealth = dashboard?.learning_health?.ml ?? dashboard?.learning_health ?? {};

  const liveAssets = useMemo(() => {
    const rows = [];
    for (const [epic, row] of Object.entries(microBackend)) {
      if (!row || typeof row !== "object") continue;
      rows.push({
        epic,
        label: epicLabel(epic),
        regime: row.regime ?? "—",
        confidence: row.confidence,
        spreadZ: row.spread_z,
        blocked: row.blocked,
      });
    }
    if (rows.length) return rows;
    const assets = telemetry?.assets ?? {};
    return Object.entries(assets).map(([key, slice]) => ({
      epic: slice?.epic ?? key,
      label: ASSET_LABELS[key] ?? key,
      regime: slice?.direction ?? "—",
      confidence: slice?.confidence,
      spreadZ: slice?.spread,
      blocked: Boolean(slice?.blocker),
    }));
  }, [microBackend, telemetry?.assets]);

  const handleRunReplay = async () => {
    setReplayRunError(null);
    setReplayRunning(true);
    const result = await triggerReplay();
    if (!result?.ok) {
      setReplayRunning(false);
      setReplayRunError(
        result?.error ||
          (result?.status === 409 ? "Outside 07:00–22:30 London" : "Failed to start replay"),
      );
      return;
    }
    await load();
    setReplayRunning(false);
  };

  const replayMeta =
    replay?.calibrationFactor != null ? calibrationMeta(replay.calibrationFactor) : null;
  const shadowMeta = shadow ? signalQualityMeta(shadow.firedRate) : null;
  const hasLive = liveAssets.length > 0 || funnel != null;

  return (
    <div className="apex-intel-shell">
      <header className="apex-intel-shell__header">
        <div>
          <h1 className="apex-intel-shell__title">Intelligence Plane</h1>
          <p className="apex-intel-shell__sub">
            Live microstructure · shadow signal quality · ML learning path
          </p>
        </div>
        <button
          type="button"
          className="apex-intel-refresh"
          onClick={load}
          disabled={loading}
        >
          Refresh
        </button>
      </header>

      {error && (
        <GlassCard title="Sync status">
          <p className="apex-hud-backfill">{error}</p>
        </GlassCard>
      )}

      <GlassCard title="Live Microstructure" tag="Real-time">
        {!hasLive && loading ? (
          <p className="apex-intel-placeholder">
            <span className="apex-intel-pulse" aria-hidden="true" />
            {EMPTY_MSG}
          </p>
        ) : liveAssets.length === 0 ? (
          <p className="apex-intel-placeholder">{EMPTY_MSG}</p>
        ) : (
          <ul className="apex-intel-micro-list">
            {liveAssets.map((row) => (
              <li key={row.epic} className="apex-intel-micro-row">
                <span className="apex-intel-micro-label">{row.label}</span>
                <span className="apex-intel-micro-regime">{row.regime}</span>
                <span className="apex-intel-micro-conf">
                  {row.confidence != null ? `${Number(row.confidence).toFixed(1)}%` : "—"}
                </span>
                {row.blocked && (
                  <span className="apex-intel-micro-block">SPREAD BLOCK</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {funnel && (
          <div className="apex-intel-funnel-strip">
            <span>Scanned {(funnel?.opportunities_scanned ?? 0).toLocaleString()}</span>
            <span>Spread ↯ {(funnel?.spread_rejections ?? 0).toLocaleString()}</span>
            <span>Liquidity ↯ {(funnel?.liquidity_blocks ?? 0).toLocaleString()}</span>
            <span>ML veto {(funnel?.ml_veto_flags ?? 0).toLocaleString()}</span>
          </div>
        )}
      </GlassCard>

      <div className="apex-intel-grid">
        <GlassCard title="Replay Summary" tag="OHLC pipeline" loading={loading && !dashboard}>
          <p className="apex-intel-replay-status">
            Backtest Core: ONLINE · Ready to ingest tick logs
          </p>
          <div className="apex-intel-actions">
            <button
              type="button"
              onClick={handleRunReplay}
              disabled={!replayWindowOpen || replayRunning}
              className="apex-intel-btn"
            >
              {replayRunning ? "Running…" : "Run replay"}
            </button>
          </div>
          {replayRunError && (
            <p className="apex-intel-muted apex-intel-muted--fail">{replayRunError}</p>
          )}
          {!replay?.lastRunTime && !loading ? (
            <p className="apex-intel-muted">No replay run yet</p>
          ) : (
            <dl className="apex-intel-dl">
              <div>
                <dt>Last run</dt>
                <dd>{fmtTs(replay?.lastRunTime)}</dd>
              </div>
              <div>
                <dt>Bars processed</dt>
                <dd>{replay?.barsProcessed ?? "—"}</dd>
              </div>
              <div>
                <dt>Calibration</dt>
                <dd>{replayMeta ? `${replayMeta.pct.toFixed(1)}%` : "—"}</dd>
              </div>
            </dl>
          )}
          {replayMeta && (
            <StatusBadge label={replayMeta.badge} className={replayMeta.className} />
          )}
        </GlassCard>

        <GlassCard title="Signal Quality (Shadow)" tag="Shadow log" loading={loading && !dashboard}>
          {!shadow && !loading ? (
            <p className="apex-intel-placeholder">{EMPTY_MSG}</p>
          ) : (
            <div className="apex-intel-neon">
              <dl className="apex-intel-dl">
                <div>
                  <dt>Records today</dt>
                  <dd>{shadow?.recordCount ?? 0}</dd>
                </div>
                <div>
                  <dt>Would-have-fired</dt>
                  <dd>{fmtPct(shadow?.firedRate ?? 0)}</dd>
                </div>
              </dl>
              {shadowMeta && (
                <StatusBadge label={shadowMeta.badge} className={shadowMeta.className} />
              )}
              {shadow?.topBlocked && (
                <p className="apex-intel-muted">
                  Top blocked setup: <strong>{shadow.topBlocked}</strong>
                </p>
              )}
              {(shadow?.topSetups ?? []).length > 0 && (
                <ul className="apex-intel-setup-list">
                  {shadow.topSetups.map((row) => (
                    <li key={row.name}>
                      <span>{row.name}</span>
                      <span>{row.count ?? "—"}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </GlassCard>

        <GlassCard title="Learning Status" tag="ML path" loading={loading && !dashboard}>
          <div className="apex-intel-neon">
            <dl className="apex-intel-dl">
            <div>
              <dt>ML store rows</dt>
              <dd>{learning?.mlRows ?? 0}</dd>
            </div>
            <div>
              <dt>Confirmed trades</dt>
              <dd>{learning?.confirmed ?? 0}</dd>
            </div>
            <div>
              <dt>Model blend</dt>
              <dd>{mlHealth?.blend_active ? "ACTIVE" : mlHealth?.ready ? "READY" : "WARMING"}</dd>
            </div>
          </dl>
          <div className="apex-intel-progress-track">
            <div
              className="apex-intel-progress-fill"
              style={{ width: `${(learning?.progress ?? 0) * 100}%` }}
            />
          </div>
          <p className="apex-intel-muted">
            {learning?.confirmed ?? 0} / {learning?.target ?? 500} toward full model confidence
          </p>
          {(learning?.topSetups ?? []).length > 0 ? (
            <table className="apex-intel-table">
              <thead>
                <tr>
                  <th>Setup</th>
                  <th>Trades</th>
                  <th>Win rate</th>
                </tr>
              </thead>
              <tbody>
                {learning.topSetups.map((row) => (
                  <tr key={row.setup}>
                    <td>{row.setup}</td>
                    <td>{row.trades}</td>
                    <td>{fmtWinRate(row.winRate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="apex-intel-placeholder">No setup stats yet</p>
          )}
          </div>
        </GlassCard>
      </div>

      {dashboard?.generated_at && (
        <p className="apex-intel-ts">
          Snapshot {fmtTs(dashboard.generated_at)}
        </p>
      )}
    </div>
  );
}
