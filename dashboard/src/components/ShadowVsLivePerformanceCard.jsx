import { memo, useCallback, useEffect, useState } from "react";

function parseContentDispositionFilename(header) {
  if (!header) return "shadow_registry_export.csv";
  const match = /filename="([^"]+)"/i.exec(header);
  return match?.[1] || "shadow_registry_export.csv";
}

async function downloadShadowRegistryCsv() {
  const res = await fetch("/api/admin/export-shadow?download=true");
  if (!res.ok) {
    const text = await res.text();
    let message = text || res.statusText;
    try {
      const body = JSON.parse(text);
      if (body?.detail) {
        message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      /* use raw text */
    }
    throw new Error(message);
  }
  const blob = await res.blob();
  const filename = parseContentDispositionFilename(
    res.headers.get("Content-Disposition"),
  );
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
  return filename;
}

function ExportShadowLogsButton() {
  const [exporting, setExporting] = useState(false);
  const [feedback, setFeedback] = useState("");

  const handleExport = useCallback(async () => {
    if (exporting) return;
    setExporting(true);
    setFeedback("");
    try {
      const filename = await downloadShadowRegistryCsv();
      setFeedback(`Saved ${filename}`);
    } catch (err) {
      setFeedback(err?.message || "Export failed");
    } finally {
      setExporting(false);
    }
  }, [exporting]);

  return (
    <div className="mt-4 flex flex-col items-center gap-2 border-t border-border/60 pt-4">
      <button
        type="button"
        onClick={handleExport}
        disabled={exporting}
        aria-busy={exporting}
        className={[
          "inline-flex min-h-[2.5rem] items-center justify-center gap-2 rounded-lg border px-4 py-2",
          "text-[11px] font-semibold uppercase tracking-wide transition-colors sm:text-xs",
          exporting
            ? "cursor-wait border-accent/40 bg-accent/10 text-accent"
            : "border-accent/50 bg-accent/10 text-accent hover:border-accent hover:bg-accent/20",
          "disabled:cursor-wait disabled:opacity-90",
        ].join(" ")}
      >
        {exporting ? (
          <>
            <span
              className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-accent/30 border-t-accent"
              aria-hidden
            />
            Exporting…
          </>
        ) : (
          <>
            <svg
              className="h-4 w-4 shrink-0"
              viewBox="0 0 20 20"
              fill="none"
              aria-hidden
            >
              <path
                d="M10 3v9m0 0l3-3m-3 3L7 9M4 14v1a1 1 0 001 1h10a1 1 0 001-1v-1"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Export Shadow Logs (.CSV)
          </>
        )}
      </button>
      {feedback && (
        <p
          className={[
            "max-w-full text-center text-[10px] leading-snug sm:text-[11px]",
            feedback.startsWith("Saved") ? "text-success" : "text-danger",
          ].join(" ")}
        >
          {feedback}
        </p>
      )}
    </div>
  );
}

function fmtPct(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function fmtProfitFactor(value) {
  if (value == null) return "∞";
  if (Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  if (n <= 0) return "0.00";
  return n.toFixed(2);
}

function fmtGbp(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `£${Number(value).toFixed(2)}`;
}

function resolveShadowVsLive(state) {
  const fromState = state?.metrics?.shadow_vs_live;
  if (fromState && (fromState.live || fromState.shadow)) {
    return fromState;
  }
  const fromHealth = state?.system_status?.metrics?.shadow_vs_live;
  if (fromHealth && (fromHealth.live || fromHealth.shadow)) {
    return fromHealth;
  }
  const legacy = state?.system_status?.shadow_analytics;
  if (legacy?.agent_sourced || legacy?.shadow_training_registry) {
    return {
      ok: legacy.ok !== false,
      live: legacy.agent_sourced,
      shadow: legacy.shadow_training_registry,
      comparison: legacy.comparison,
    };
  }
  return null;
}

function metricsFingerprint(payload) {
  if (!payload) return "";
  const live = payload.live || {};
  const shadow = payload.shadow || {};
  return [
    live.trade_count,
    live.win_rate,
    live.profit_factor,
    live.average_drawdown_gbp,
    live.net_pnl_gbp,
    shadow.trade_count,
    shadow.win_rate,
    shadow.profit_factor,
    shadow.average_drawdown_gbp,
    shadow.net_pnl_gbp,
    payload.ok,
  ].join("|");
}

function MetricCell({ label, liveValue, shadowValue, format }) {
  const liveText = format(liveValue);
  const shadowText = format(shadowValue);
  return (
    <div className="grid grid-cols-3 items-center gap-2 border-b border-border/60 py-3 last:border-b-0">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted sm:text-xs">
        {label}
      </span>
      <div className="text-center">
        <span className="block text-[10px] uppercase tracking-wide text-success/80">Live</span>
        <span className="mt-0.5 block font-mono text-base font-bold tabular-nums text-foreground sm:text-lg">
          {liveText}
        </span>
      </div>
      <div className="text-center">
        <span className="block text-[10px] uppercase tracking-wide text-muted">Shadow</span>
        <span className="mt-0.5 block font-mono text-base font-semibold tabular-nums text-muted sm:text-lg">
          {shadowText}
        </span>
      </div>
    </div>
  );
}

export default memo(function ShadowVsLivePerformanceCard({ state }) {
  const [payload, setPayload] = useState(() => resolveShadowVsLive(state));

  useEffect(() => {
    const direct = resolveShadowVsLive(state);
    if (direct?.live || direct?.shadow) {
      setPayload((prev) => {
        if (metricsFingerprint(prev) === metricsFingerprint(direct)) return prev;
        return direct;
      });
      return undefined;
    }
    let cancelled = false;
    fetch("/api/learning-health")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (cancelled || !data?.shadow_analytics) return;
        const sa = data.shadow_analytics;
        setPayload({
          ok: sa.ok !== false,
          live: sa.live,
          shadow: sa.shadow,
          comparison: sa.comparison,
        });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [state]);

  if (!payload) {
    return (
      <section className="mx-auto w-full max-w-3xl rounded-lg border border-border bg-card p-4 sm:p-5">
        <h2 className="label-caps mb-2 text-center">Shadow vs live performance</h2>
        <p className="text-center text-[12px] text-muted">Loading analytics…</p>
      </section>
    );
  }

  if (payload.ok === false) {
    return (
      <section className="mx-auto w-full max-w-3xl rounded-lg border border-border bg-card p-4 sm:p-5">
        <h2 className="label-caps mb-2 text-center">Shadow vs live performance</h2>
        <p className="text-center text-[12px] text-danger">{payload.error || "Unavailable"}</p>
      </section>
    );
  }

  const live = payload.live || {};
  const shadow = payload.shadow || {};

  return (
    <section className="mx-auto w-full max-w-3xl rounded-lg border border-border bg-card p-4 sm:p-5">
      <div className="mb-4 text-center">
        <h2 className="label-caps">Shadow vs live performance</h2>
        <p className="mt-1 text-[11px] leading-snug text-muted">
          Agent-sourced closes vs IG-import shadow registry — live gates ignore shadow rows.
        </p>
      </div>

      <div className="mb-3 grid grid-cols-3 gap-2 text-center text-[10px] uppercase tracking-wide text-muted">
        <span className="text-left">Metric</span>
        <span>
          Live
          <span className="mt-0.5 block font-normal normal-case text-[10px] text-muted">
            {live.trade_count ?? 0} trades
          </span>
        </span>
        <span>
          Shadow
          <span className="mt-0.5 block font-normal normal-case text-[10px] text-muted">
            {shadow.trade_count ?? 0} trades
          </span>
        </span>
      </div>

      <div className="rounded-lg border border-border/80 bg-surface/40 px-3 sm:px-4">
        <MetricCell
          label="Win rate"
          liveValue={live.win_rate}
          shadowValue={shadow.win_rate}
          format={fmtPct}
        />
        <MetricCell
          label="Profit factor"
          liveValue={live.profit_factor}
          shadowValue={shadow.profit_factor}
          format={fmtProfitFactor}
        />
        <MetricCell
          label="Avg drawdown"
          liveValue={live.average_drawdown_gbp}
          shadowValue={shadow.average_drawdown_gbp}
          format={fmtGbp}
        />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-center text-[10px] text-muted sm:text-[11px]">
        <div className="rounded border border-border/60 bg-bg/40 px-2 py-1.5">
          <span className="block uppercase tracking-wide">Live net P&amp;L</span>
          <span className="mt-0.5 block font-mono text-sm font-semibold tabular-nums text-foreground">
            {fmtGbp(live.net_pnl_gbp)}
          </span>
        </div>
        <div className="rounded border border-border/60 bg-bg/40 px-2 py-1.5">
          <span className="block uppercase tracking-wide">Shadow net P&amp;L</span>
          <span className="mt-0.5 block font-mono text-sm font-semibold tabular-nums text-muted">
            {fmtGbp(shadow.net_pnl_gbp)}
          </span>
        </div>
      </div>

      <ExportShadowLogsButton />
    </section>
  );
}, (prev, next) =>
  metricsFingerprint(resolveShadowVsLive(prev.state))
  === metricsFingerprint(resolveShadowVsLive(next.state)));
