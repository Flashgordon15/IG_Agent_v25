"use client";

import { useEffect, useMemo, useState } from "react";
import {
  CFD_API_DEFAULT,
  SB_API_DEFAULT,
  cfdHttpBase,
  fetchDeskJson,
  sbHttpBase,
} from "@/lib/desk-api-bases";
import {
  BOOT_LATENCY_BUFFER_SEC,
  BOOT_SOT_GRACE_SEC,
  isTradeSupportSotFresh,
} from "@/lib/desk-multiplex";

export type BootCheck = {
  id: string;
  status: "pass" | "fail" | "warn" | "healing" | string;
  detail?: string;
};

export type BootGate = {
  ready_for_desk?: boolean;
  /** UI unlock — supervisors OK even when entries paused / REST amber. */
  ready_for_view?: boolean;
  boot_started_at?: number;
  elapsed_sec?: number;
  boot_grace_active?: boolean;
  boot_latency_buffer_sec?: number;
  boot_latency_buffer_active?: boolean;
  false_engine_blockage_suppressed?: boolean;
  sot_stale_budget_sec?: number;
  /** Stale-cache circuit breaker hydrated SoT from broker_snapshot.json. */
  boot_sot_fallback?: boolean;
  boot_sot_fallback_reason?: string | null;
  boot_sot_soft_fail?: boolean;
  checks?: BootCheck[];
  healing_actions?: string[];
  blockers?: string[];
  upgrades_live?: Array<{
    id?: string;
    date?: string;
    title?: string;
    detail?: string;
  }>;
  stuck?: boolean;
  operator_hints?: string[];
  promise?: string;
};

type StabilityPayload = {
  ok?: boolean;
  ready_for_desk?: boolean;
  ready_for_view?: boolean;
  boot_gate?: BootGate;
  desk_stability?: {
    grade?: string;
    label?: string;
    boot_gate?: BootGate;
    feed?: string;
    components?: {
      sot_ok?: boolean;
      flat_book?: boolean;
      cap_breach?: boolean;
      has_open_risk?: boolean;
      trade_support_running?: boolean;
      broker_open?: number;
    };
  };
  grade?: string;
  label?: string;
};

type Props = {
  /** When true, call onReady once gate clears (home overlay mode). */
  onReady?: () => void;
  /** Dedicated /boot page: navigate to desk after ready. */
  redirectOnReady?: boolean;
  pollMs?: number;
};

/** Soft view-unlock blockers — operational pauses, not harness-down. */
const VIEW_SOFT_BLOCKERS = new Set([
  "path_not_live",
  "rest_critical",
  "rest_high",
  "rest_elevated",
  "entries_paused",
  "manual_stop",
  "trade_ready_false",
  "agent_not_ready",
]);

const STABILITY_TIMEOUT_MS = 9000;

function statusGlyph(status: string): string {
  if (status === "pass") return "●";
  if (status === "healing") return "◌";
  if (status === "warn") return "▲";
  return "■";
}

function statusClass(status: string): string {
  if (status === "pass") return "boot-check boot-check--pass";
  if (status === "healing") return "boot-check boot-check--heal";
  if (status === "warn") return "boot-check boot-check--warn";
  return "boot-check boot-check--fail";
}

function fmtClock(tsSec: number | undefined): string {
  if (!tsSec || !Number.isFinite(tsSec)) return "—";
  // Agent may emit unix seconds; if looks like ms, normalize.
  const ms = tsSec > 1e12 ? tsSec : tsSec * 1000;
  try {
    return new Date(ms).toLocaleString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      day: "2-digit",
      month: "short",
    });
  } catch {
    return "—";
  }
}

function gateOf(body: StabilityPayload | null | undefined): BootGate | undefined {
  return body?.boot_gate || body?.desk_stability?.boot_gate;
}

function checkStatus(gate: BootGate | undefined, id: string): string | undefined {
  return gate?.checks?.find((c) => c.id === id)?.status;
}

/**
 * Unlock the Trading Desk UI when either twin can supervise the book.
 * ready_for_desk (entry-armed) remains strict; view unlock tolerates A2 pause,
 * REST amber, and dual-desk where one twin is down.
 */
export function isDeskViewReady(body: StabilityPayload | null | undefined): boolean {
  if (!body) return false;
  const gate = gateOf(body);
  if (body.ready_for_desk === true || gate?.ready_for_desk === true) return true;
  if (body.ready_for_view === true || gate?.ready_for_view === true) return true;

  const comps = body.desk_stability?.components;
  if (comps?.cap_breach === true) return false;

  const sot =
    checkStatus(gate, "trade_support_sot") === "pass" ||
    comps?.sot_ok === true ||
    (gate?.boot_sot_fallback === true &&
      (checkStatus(gate, "trade_support_sot") === "warn" ||
        checkStatus(gate, "trade_support_sot") === "pass"));
  if (!sot) return false;

  const hard = (gate?.blockers || []).filter((b) => !VIEW_SOFT_BLOCKERS.has(b));
  if (hard.includes("cap_breach") || hard.includes("healing_in_progress")) {
    return false;
  }
  if (hard.includes("sot_stale") || hard.includes("opm_unhealthy")) return false;
  if (hard.includes("liveness_degraded") || hard.includes("trade_support_down")) {
    return false;
  }

  // Grade R alone is not enough to freeze the viewer when SoT is flat and soft.
  const grade = String(
    body.desk_stability?.grade || body.grade || "",
  ).toUpperCase();
  if (grade === "R" && hard.includes("desk_critical")) {
    // Still allow view if book is flat / no open risk.
    const flat =
      comps?.flat_book === true ||
      Number(comps?.broker_open ?? 1) === 0 ||
      comps?.has_open_risk === false;
    if (!flat) return false;
  }

  return true;
}

type StabilityProbe = {
  body: StabilityPayload | null;
  source: string;
  err: string | null;
};

async function probeStability(
  base: string,
  label: string,
): Promise<StabilityProbe> {
  try {
    const body = await fetchDeskJson<StabilityPayload>(
      base,
      "/api/desk/stability",
      undefined,
      STABILITY_TIMEOUT_MS,
    );
    return { body, source: label, err: null };
  } catch (e) {
    return {
      body: null,
      source: label,
      err: e instanceof Error ? e.message : `${label} probe failed`,
    };
  }
}

async function probeHealth(base: string): Promise<boolean> {
  try {
    const h = await fetchDeskJson<{ ok?: boolean; status?: string }>(
      base,
      "/api/health",
      undefined,
      5000,
    );
    return h?.ok === true || String(h?.status || "").toLowerCase() === "ok";
  } catch {
    return false;
  }
}

/**
 * Dual-desk stability: prefer first usable twin (CFD then SB), fall back to
 * health-only synthetic gate so arming is not wedged on a hung :8080.
 */
async function pullDualStability(): Promise<StabilityProbe> {
  const cfdBase = cfdHttpBase() || CFD_API_DEFAULT;
  const sbBase = sbHttpBase() || SB_API_DEFAULT;

  const [cfd, sb] = await Promise.all([
    probeStability(cfdBase, "CFD :8080"),
    probeStability(sbBase, "SB :8081"),
  ]);

  const prefer = (p: StabilityProbe): boolean =>
    Boolean(p.body) && (isDeskViewReady(p.body) || Boolean(gateOf(p.body)?.checks?.length));

  if (prefer(cfd) && prefer(sb)) {
    // Prefer entry-armed; else prefer SB when CFD is hung/paused-only.
    if (cfd.body?.ready_for_desk || gateOf(cfd.body)?.ready_for_desk) return cfd;
    if (sb.body?.ready_for_desk || gateOf(sb.body)?.ready_for_desk) return sb;
    if (isDeskViewReady(sb.body)) return sb;
    return cfd;
  }
  if (prefer(cfd)) return cfd;
  if (prefer(sb)) return sb;
  if (cfd.body) return cfd;
  if (sb.body) return sb;

  // Health fallback — unlock viewer when at least one twin answers /api/health.
  const [cfdHealth, sbHealth] = await Promise.all([
    probeHealth(cfdBase),
    probeHealth(sbBase),
  ]);
  if (cfdHealth || sbHealth) {
    const src = sbHealth && !cfdHealth ? "SB :8081" : "CFD :8080";
    const synthetic: StabilityPayload = {
      ok: true,
      ready_for_view: true,
      ready_for_desk: false,
      grade: "A",
      label: `A — health fallback (${src})`,
      boot_gate: {
        ready_for_view: true,
        ready_for_desk: false,
        checks: [
          {
            id: "agent_api",
            status: "pass",
            detail: `${src} /api/health ok (stability unreachable)`,
          },
          {
            id: "trade_support_sot",
            status: "warn",
            detail: "stability harness unreachable — health fallback",
          },
        ],
        blockers: ["stability_unreachable"],
        upgrades_live: [],
        promise:
          "Viewer unlocked via health fallback — entry path not certified",
        operator_hints: [
          "CFD :8080 hung or slow — SB may still supervise",
          "curl -sS -m 5 http://127.0.0.1:8080/api/desk/stability",
          "curl -sS -m 5 http://127.0.0.1:8081/api/desk/stability",
        ],
      },
    };
    return { body: synthetic, source: `${src} health`, err: null };
  }

  const errParts = [cfd.err, sb.err].filter(Boolean);
  return {
    body: null,
    source: "none",
    err:
      errParts.join(" · ") ||
      "HARNESS UNREACHABLE — both :8080 and :8081 timed out",
  };
}

export function DeskBootSplash({
  onReady,
  redirectOnReady = false,
  pollMs = 1500,
}: Props) {
  const [payload, setPayload] = useState<StabilityPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [source, setSource] = useState<string>("—");
  const [localElapsed, setLocalElapsed] = useState(0);
  const mountedAt = useMemo(() => Date.now(), []);

  useEffect(() => {
    const id = window.setInterval(() => {
      setLocalElapsed(Math.floor((Date.now() - mountedAt) / 1000));
    }, 250);
    return () => window.clearInterval(id);
  }, [mountedAt]);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const probe = await pullDualStability();
        if (!alive) return;
        if (!probe.body) {
          setErr(probe.err || "stability endpoint unreachable");
          return;
        }
        setErr(null);
        setSource(probe.source);
        setPayload(probe.body);
        if (isDeskViewReady(probe.body)) {
          onReady?.();
          if (redirectOnReady) {
            window.location.replace("/desk");
          }
        }
      } catch (e) {
        if (!alive) return;
        setErr(e instanceof Error ? e.message : "poll failed");
      }
    };
    void pull();
    const id = window.setInterval(pull, pollMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [onReady, redirectOnReady, pollMs]);

  const gate = gateOf(payload);
  const checks = gate?.checks || [];
  const upgrades = gate?.upgrades_live || [];
  const healing = gate?.healing_actions || [];
  const grade = payload?.desk_stability?.grade || payload?.grade || "—";
  const elapsed = gate?.elapsed_sec ?? localElapsed;
  const sotBudget = gate?.sot_stale_budget_sec;
  const latencyBuf =
    gate?.boot_latency_buffer_sec ?? BOOT_LATENCY_BUFFER_SEC;
  const bootGrace =
    gate?.boot_grace_active === true ||
    gate?.boot_latency_buffer_active === true ||
    elapsed < BOOT_SOT_GRACE_SEC;
  const viewReady = isDeskViewReady(payload);
  const entryReady =
    payload?.ready_for_desk === true || gate?.ready_for_desk === true;
  const checksHydrated = checks.map((c) => {
    if (c.id !== "trade_support_sot" || c.status === "pass") return c;
    // Honor server-side broker_snapshot boot fallback (no hard GATE HOLD freeze).
    if (gate?.boot_sot_fallback && (c.status === "warn" || c.status === "pass")) {
      return {
        ...c,
        detail: `${c.detail || ""} (snapshot hydrate${
          gate.boot_sot_fallback_reason
            ? `: ${gate.boot_sot_fallback_reason}`
            : ""
        })`,
      };
    }
    if (!bootGrace) return c;
    const m = /age=([\d.]+)/.exec(String(c.detail || ""));
    const age = m ? Number(m[1]) : null;
    if (isTradeSupportSotFresh(age, gate?.boot_started_at, sotBudget)) {
      return {
        ...c,
        status: "warn",
        detail: `${c.detail || ""} (boot grace ${sotBudget ?? latencyBuf}s budget)`,
      };
    }
    return c;
  });
  const stuck =
    gate?.stuck === true || (elapsed > 300 && !viewReady);
  const feed = payload?.desk_stability?.feed;

  return (
    <div className="desk-boot" role="status" aria-live="polite">
      <div className="desk-boot-grid" aria-hidden />
      <header className="desk-boot-head">
        <p className="desk-boot-kicker">IG AGENT · TRADING DESK</p>
        <h1 className="desk-boot-title">System Arming</h1>
        <p className="desk-boot-sub">
          Dashboard unlocks when either twin harness/supervisors clear for view
          — entry-armed is stricter and shown separately.
        </p>
      </header>

      <div className="desk-boot-meta">
        <div>
          <span className="desk-boot-meta-label">BOOT</span>
          <strong className="desk-boot-meta-value">
            {fmtClock(gate?.boot_started_at)}
          </strong>
        </div>
        <div>
          <span className="desk-boot-meta-label">ELAPSED</span>
          <strong className="desk-boot-meta-value desk-boot-mono">
            {Math.floor(elapsed / 60)
              .toString()
              .padStart(2, "0")}
            :{(Math.floor(elapsed) % 60).toString().padStart(2, "0")}
          </strong>
        </div>
        <div>
          <span className="desk-boot-meta-label">STABILITY</span>
          <strong
            className={`desk-boot-meta-value desk-boot-grade desk-boot-grade--${String(grade).toLowerCase()}`}
          >
            {grade}
          </strong>
        </div>
        <div>
          <span className="desk-boot-meta-label">GATE</span>
          <strong className="desk-boot-meta-value">
            {entryReady ? "ENTRY CLEARED" : viewReady ? "VIEW CLEARED" : "HOLD"}
          </strong>
        </div>
        <div>
          <span className="desk-boot-meta-label">SOURCE</span>
          <strong className="desk-boot-meta-value desk-boot-mono">{source}</strong>
        </div>
      </div>

      {healing.length > 0 ? (
        <div className="desk-boot-heal" role="alert">
          SELF-HEAL IN PROGRESS — {healing.join(" · ")}
        </div>
      ) : null}

      {err ? (
        <div className="desk-boot-heal desk-boot-heal--fail" role="alert">
          HARNESS UNREACHABLE — {err}
        </div>
      ) : null}

      <div className="desk-boot-panels">
        <section className="desk-boot-panel">
          <h2 className="desk-boot-panel-title">Harness checks</h2>
          <ul className="desk-boot-checks">
            {checks.length === 0 ? (
              <li className="boot-check boot-check--warn">
                <span>◌</span>
                <div>
                  <strong>waiting for /api/desk/stability</strong>
                  <p>Polling CFD :8080 + SB :8081…</p>
                </div>
              </li>
            ) : (
              checksHydrated.map((c) => (
                <li key={c.id} className={statusClass(String(c.status))}>
                  <span aria-hidden>{statusGlyph(String(c.status))}</span>
                  <div>
                    <strong>{c.id}</strong>
                    <p>{c.detail || String(c.status)}</p>
                  </div>
                </li>
              ))
            )}
          </ul>
          {feed ? (
            <p className="desk-boot-feed desk-boot-mono">{feed}</p>
          ) : null}
        </section>

        <section className="desk-boot-panel">
          <h2 className="desk-boot-panel-title">Upgrades live</h2>
          <ul className="desk-boot-upgrades">
            {upgrades.length === 0 ? (
              <li className="desk-boot-upgrade">
                <span className="desk-boot-mono">—</span>
                <div>
                  <strong>Manifest loading…</strong>
                </div>
              </li>
            ) : (
              upgrades.map((u) => (
                <li key={u.id || u.title} className="desk-boot-upgrade">
                  <span className="desk-boot-mono">{u.date || "—"}</span>
                  <div>
                    <strong>{u.title}</strong>
                    {u.detail ? <p>{u.detail}</p> : null}
                  </div>
                </li>
              ))
            )}
          </ul>
        </section>
      </div>

      {stuck ? (
        <section className="desk-boot-stuck" role="alert">
          <h2>Gate stuck — do not assume trading is armed</h2>
          <ul>
            {(gate?.operator_hints || []).map((h) => (
              <li key={h} className="desk-boot-mono">
                {h}
              </li>
            ))}
            {(gate?.blockers || []).length > 0 ? (
              <li className="desk-boot-mono">
                blockers: {(gate?.blockers || []).join(", ")}
              </li>
            ) : null}
          </ul>
        </section>
      ) : (
        <p className="desk-boot-foot desk-boot-mono">
          {gate?.promise ||
            "ready_for_desk = path armed for entries when signals fire — not an immediate fill"}
        </p>
      )}
    </div>
  );
}
