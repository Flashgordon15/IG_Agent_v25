"use client";

/**
 * v36 — vertical core affinity micro-indicators (CFD→Core 1 · SB→Core 2).
 * Ref-driven DOM paint; polls ops_strip / health when affinity fields exist.
 */

import { useEffect, useRef } from "react";
import { cfdHttpBase, fetchDeskJson, sbHttpBase } from "@/lib/desk-api-bases";
import { fetchAgentJson } from "@/lib/agent-client";

type AffinityLane = {
  id: "cfd" | "sb";
  label: string;
  accountId: string;
  targetCore: number;
  pinned: boolean | null;
  reason: string;
};

type OpsAffinity = {
  core_affinity?: {
    pinned?: boolean;
    core?: number;
    origin?: string;
    reason?: string;
  };
  hardware_affinity?: {
    pinned?: boolean;
    core?: number;
    reason?: string;
  };
  orchestrator?: {
    engine_origin?: string;
    core?: number;
  };
};

const PIN_TARGETS: Record<"cfd" | "sb", Omit<AffinityLane, "pinned" | "reason">> = {
  cfd: {
    id: "cfd",
    label: "QUANT SNIPER",
    accountId: "Z6BAH4",
    targetCore: 1,
  },
  sb: {
    id: "sb",
    label: "MACRO SENTINEL",
    accountId: "Z6BAH3",
    targetCore: 2,
  },
};

const POLL_MS = 10_000;

function resolveLane(
  port: "cfd" | "sb",
  payload: OpsAffinity | null,
  online: boolean,
): AffinityLane {
  const base = PIN_TARGETS[port];
  const aff = payload?.core_affinity ?? payload?.hardware_affinity;
  let pinned: boolean | null = aff?.pinned ?? null;
  let reason = String(aff?.reason || "");

  if (aff?.core != null && aff.core !== base.targetCore) {
    reason = reason || `core ${aff.core}`;
  }
  if (!online) {
    pinned = false;
    reason = reason || "port offline";
  }
  if (!reason) {
    reason = pinned === true ? "pinned" : pinned === false ? "intent only" : "configured pin";
  }

  return {
    ...base,
    pinned,
    reason,
  };
}

type MeterRefs = {
  fill: HTMLDivElement | null;
  core: HTMLSpanElement | null;
  status: HTMLSpanElement | null;
};

function paintMeter(refs: MeterRefs, lane: AffinityLane): void {
  const pct =
    lane.pinned === true ? 100 : lane.pinned === false ? 42 : 58;
  if (refs.fill) {
    refs.fill.style.height = `${pct}%`;
    refs.fill.dataset.tone =
      lane.pinned === true ? "live" : lane.pinned === false ? "intent" : "unknown";
  }
  if (refs.core) {
    refs.core.textContent = `CORE ${lane.targetCore}`;
  }
  if (refs.status) {
    refs.status.textContent =
      lane.pinned === true
        ? "PINNED"
        : lane.pinned === false
          ? "NO-OP"
          : "TARGET";
    refs.status.dataset.tone =
      lane.pinned === true ? "live" : lane.pinned === false ? "mute" : "warn";
  }
}

export function HardwareAffinityMeters() {
  const cfdRefs = useRef<MeterRefs>({ fill: null, core: null, status: null });
  const sbRefs = useRef<MeterRefs>({ fill: null, core: null, status: null });
  const cfdAcctRef = useRef<HTMLSpanElement | null>(null);
  const sbAcctRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    let alive = true;
    const cfdBase = cfdHttpBase();
    const sbBase = sbHttpBase();

    const pull = async () => {
      const [cfdOps, sbOps, cfdHealth, sbHealth] = await Promise.all([
        fetchDeskJson<OpsAffinity>(cfdBase, "/api/desk/ops_strip", undefined, 2500)
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
        fetchDeskJson<OpsAffinity>(sbBase, "/api/desk/ops_strip", undefined, 2500)
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
        fetchAgentJson<OpsAffinity>("/api/health", undefined, 2500)
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
        fetchDeskJson<OpsAffinity>(sbBase, "/api/health", undefined, 2500)
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
      ]);
      if (!alive) return;

      const cfdPayload = cfdOps.payload ?? cfdHealth.payload;
      const sbPayload = sbOps.payload ?? sbHealth.payload;
      const cfdLane = resolveLane("cfd", cfdPayload, cfdOps.ok || cfdHealth.ok);
      const sbLane = resolveLane("sb", sbPayload, sbOps.ok || sbHealth.ok);

      paintMeter(cfdRefs.current, cfdLane);
      paintMeter(sbRefs.current, sbLane);
      if (cfdAcctRef.current) {
        cfdAcctRef.current.textContent = `${cfdLane.label} · ${cfdLane.accountId} → C${cfdLane.targetCore}`;
      }
      if (sbAcctRef.current) {
        sbAcctRef.current.textContent = `${sbLane.label} · ${sbLane.accountId} → C${sbLane.targetCore}`;
      }
    };

    void pull();
    const id = window.setInterval(pull, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  return (
    <section className="v36-affinity-panel" aria-label="Hardware core affinity">
      <header className="v36-strip-head">
        <p className="gpu-kicker">Hardware Affinity</p>
        <h3 className="v36-strip-title">Core Pin Targets</h3>
      </header>
      <div className="v36-affinity-grid">
        <div className="v36-affinity-lane">
          <span ref={cfdAcctRef} className="v36-affinity-label">
            QUANT SNIPER · Z6BAH4 → C1
          </span>
          <div className="v36-affinity-meter" aria-hidden>
            <div
              ref={(el) => {
                cfdRefs.current.fill = el;
              }}
              className="v36-affinity-fill"
              data-tone="unknown"
            />
          </div>
          <div className="v36-affinity-meta">
            <span
              ref={(el) => {
                cfdRefs.current.core = el;
              }}
              className="v36-affinity-core"
            >
              CORE 1
            </span>
            <span
              ref={(el) => {
                cfdRefs.current.status = el;
              }}
              className="v36-affinity-status"
              data-tone="unknown"
            >
              TARGET
            </span>
          </div>
        </div>
        <div className="v36-affinity-lane">
          <span ref={sbAcctRef} className="v36-affinity-label">
            MACRO SENTINEL · Z6BAH3 → C2
          </span>
          <div className="v36-affinity-meter" aria-hidden>
            <div
              ref={(el) => {
                sbRefs.current.fill = el;
              }}
              className="v36-affinity-fill"
              data-tone="unknown"
            />
          </div>
          <div className="v36-affinity-meta">
            <span
              ref={(el) => {
                sbRefs.current.core = el;
              }}
              className="v36-affinity-core"
            >
              CORE 2
            </span>
            <span
              ref={(el) => {
                sbRefs.current.status = el;
              }}
              className="v36-affinity-status"
              data-tone="unknown"
            >
              TARGET
            </span>
          </div>
        </div>
      </div>
    </section>
  );
}
