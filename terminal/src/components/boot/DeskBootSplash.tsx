"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchAgentJson } from "@/lib/agent-client";
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
  boot_gate?: BootGate;
  desk_stability?: {
    grade?: string;
    label?: string;
    boot_gate?: BootGate;
    feed?: string;
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

export function DeskBootSplash({
  onReady,
  redirectOnReady = false,
  pollMs = 1500,
}: Props) {
  const [payload, setPayload] = useState<StabilityPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
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
        const body = await fetchAgentJson<StabilityPayload>(
          "/api/desk/stability",
          undefined,
          4000,
        );
        if (!alive) return;
        if (!body) {
          setErr("stability endpoint unreachable");
          return;
        }
        setErr(null);
        setPayload(body);
        const gate = body.boot_gate || body.desk_stability?.boot_gate;
        const ready =
          body.ready_for_desk === true || gate?.ready_for_desk === true;
        if (ready) {
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

  const gate = payload?.boot_gate || payload?.desk_stability?.boot_gate;
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
  const stuck = gate?.stuck === true || (elapsed > 300 && !gate?.ready_for_desk);
  const feed = payload?.desk_stability?.feed;

  return (
    <div className="desk-boot" role="status" aria-live="polite">
      <div className="desk-boot-grid" aria-hidden />
      <header className="desk-boot-head">
        <p className="desk-boot-kicker">IG AGENT · TRADING DESK</p>
        <h1 className="desk-boot-title">System Arming</h1>
        <p className="desk-boot-sub">
          Dashboard unlocks only when the AI harness clears path, SoT, and
          supervisors — not a fill guarantee.
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
            {gate?.ready_for_desk ? "CLEARED" : "HOLD"}
          </strong>
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
                  <p>Polling agent harness…</p>
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
