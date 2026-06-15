import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client.js";

const POLL_MS = 1000;
const MISMATCH_MINUTES = 5;
const BOUNDARY_WARN_MINUTES = 30;

const STATUS_CLASS = {
  green: "text-success border-success/40 bg-success/10",
  amber: "text-warning border-warning/40 bg-warning/10",
  red: "text-danger border-danger/40 bg-danger/10",
};

function sessionLabel(session) {
  const key = String(session ?? "").trim();
  if (!key) return "";
  return key.replace(/_/g, " ");
}

function clockMismatchMinutes(utcEpoch) {
  if (utcEpoch == null || !Number.isFinite(Number(utcEpoch))) return 0;
  const apiMs = Number(utcEpoch) * 1000;
  const browserMs = Date.now();
  return Math.abs(browserMs - apiMs) / 60000;
}

function boundaryVerb(boundaryType) {
  const t = String(boundaryType ?? "").toUpperCase();
  if (t === "OPEN") return "opens";
  if (t === "CLOSE") return "closes";
  return null;
}

/**
 * Live BST clock sourced from GET /api/time (agent timezone, not browser local).
 */
export default function BstClock() {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    const poll = async () => {
      try {
        const data = await api.time();
        if (!cancelled && data) {
          setPayload(data);
          setError(false);
        }
      } catch {
        if (!cancelled) setError(true);
      }
      if (!cancelled) {
        timer = window.setTimeout(poll, POLL_MS);
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  const mismatch = useMemo(
    () => clockMismatchMinutes(payload?.utc_epoch),
    [payload?.utc_epoch],
  );

  const boundaryMinutes = Number(payload?.minutes_to_boundary);
  const nearBoundary =
    Number.isFinite(boundaryMinutes) &&
    boundaryMinutes > 0 &&
    boundaryMinutes < BOUNDARY_WARN_MINUTES;

  if (!payload && !error) {
    return (
      <div className="inline-flex shrink-0 items-center rounded-md border border-border bg-card/60 px-2 py-1 text-[10px] text-muted">
        BST …
      </div>
    );
  }

  if (error && !payload) {
    return (
      <div className="inline-flex shrink-0 items-center rounded-md border border-border bg-card/60 px-2 py-1 text-[10px] text-muted">
        BST unavailable
      </div>
    );
  }

  let status = String(payload.clock_status ?? "green").toLowerCase();
  if (nearBoundary && status === "green") {
    status = "amber";
  }
  const statusClass = STATUS_CLASS[status] ?? STATUS_CLASS.green;
  const sess = sessionLabel(payload.session);
  const bstShort = String(payload.bst ?? "").slice(0, 5);
  const verb = boundaryVerb(payload.boundary_type);
  const boundaryLine =
    verb && Number.isFinite(boundaryMinutes)
      ? `${verb} in ${boundaryMinutes}m`
      : null;

  return (
    <div className="inline-flex shrink-0 flex-col gap-0.5">
      <div
        className={[
          "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 tabular-nums",
          statusClass,
        ].join(" ")}
        title={`Agent time (Europe/London) · session: ${sess || "—"}`}
      >
        <span className="text-[11px] font-semibold leading-none">
          {bstShort} BST
        </span>
        {sess ? (
          <span className="text-[10px] opacity-90">| {sess}</span>
        ) : null}
        {boundaryLine ? (
          <span className="text-[10px] opacity-90">| {boundaryLine}</span>
        ) : null}
      </div>
      {mismatch > MISMATCH_MINUTES ? (
        <span
          className="inline-flex items-center rounded border border-danger/50 bg-danger/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-danger"
          role="alert"
        >
          Clock mismatch
        </span>
      ) : null}
    </div>
  );
}
