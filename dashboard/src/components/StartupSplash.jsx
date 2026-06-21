import { useEffect, useRef, useState } from "react";
import { api, authHeaders } from "../api/client";
import { subscribeWarmup } from "../api/apexIpc.js";
import { subscribeBootPhase } from "../api/apexBootPhase.js";
import { isLiveVanguardPort, recoveryHealthUrl, resolveTargetPort } from "../config.js";

const POLL_MS = 400;
const API_GRACE_MS = 18000;
const MAX_API_FAILURES = 12;

const BOOT_PHASES = [
  { id: 1, label: "[WIP] Forcefully evicting stale port processes & cleaning staging tracks..." },
  { id: 2, label: "[WIP] Initializing background python microkernel on port 9090..." },
  { id: 3, label: "[WIP] Parallel 4-Worker Thread Pool: Hydrating 120-bar Yahoo historical candle arrays..." },
];

function isBackendBootComplete(boot) {
  if (!boot || typeof boot !== "object") return false;
  const stage = String(boot.stage || "").toLowerCase();
  if (stage === "warming" || boot.warming) return false;
  if (boot.ready === true && Number(boot.percent) >= 100) return true;
  return (stage === "ready" || stage === "active") && boot.ready === true;
}

function formatVectorWarmupLabel(warm) {
  if (!warm || typeof warm !== "object") {
    return "Compiling Vector Arrays: 0 / 256 Bars… [0%]";
  }
  const compiled = Number(warm.bars_compiled) || 0;
  const target = Number(warm.bars_target) || 256;
  const pct = Number(warm.percent) || Math.min(100, Math.round((compiled * 100) / Math.max(1, target)));
  return `Compiling Vector Arrays: ${compiled} / ${target} Bars… [${pct}%]`;
}

function applyWarmupPayload(setPercent, setLabel, warm) {
  if (!warm || typeof warm !== "object") return;
  const compiled = Number(warm.bars_compiled) || 0;
  const target = Number(warm.bars_target) || 256;
  const pct = Number(warm.percent) || Math.min(100, Math.round((compiled * 100) / Math.max(1, target)));
  setPercent(pct);
  setLabel(formatVectorWarmupLabel(warm));
}

async function probeHealth200() {
  try {
    const res = await fetch(recoveryHealthUrl(), {
      method: "GET",
      credentials: "include",
      headers: authHeaders(),
      signal: AbortSignal.timeout(4000),
    });
    return res.status === 200;
  } catch {
    return false;
  }
}

/**
 * Stage 2 — daemon supervisor HUD + vector warmup (IPC + /api/startup/status).
 */
export default function StartupSplash({ onComplete }) {
  const [percent, setPercent] = useState(0);
  const [label, setLabel] = useState("Awaiting daemon supervisor handshake…");
  const [error, setError] = useState(null);
  const [activePhase, setActivePhase] = useState(0);
  const [completedPhases, setCompletedPhases] = useState(() => new Set());
  const completedRef = useRef(false);
  const bootStartRef = useRef(Date.now());
  const apiFailRef = useRef(0);

  const markPhaseDone = (phaseId) => {
    if (!phaseId) return;
    setCompletedPhases((prev) => new Set([...prev, phaseId]));
    setActivePhase((cur) => Math.max(cur, phaseId));
  };

  const snapComplete = () => {
    if (completedRef.current) return;
    completedRef.current = true;
    setPercent(100);
    setLabel("Flight deck ready — daemon HTTP 200 confirmed");
    markPhaseDone(1);
    markPhaseDone(2);
    markPhaseDone(3);
    onComplete?.();
  };

  useEffect(() => {
    const unsubBoot = subscribeBootPhase((payload) => {
      const phase = Number(payload?.phase) || 0;
      if (phase >= 1) {
        setActivePhase(phase);
        for (let p = 1; p < phase; p += 1) markPhaseDone(p);
      }
      if (phase === 3) markPhaseDone(1);
    });
    return unsubBoot;
  }, []);

  useEffect(() => {
    const unsub = subscribeWarmup((payload) => {
      if (completedRef.current) return;
      setError(null);
      apiFailRef.current = 0;
      markPhaseDone(3);
      applyWarmupPayload(setPercent, setLabel, payload);
    });
    return unsub;
  }, []);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      if (cancelled || completedRef.current) return;

      if (await probeHealth200()) {
        snapComplete();
        return;
      }

      try {
        const status = await api.getStartupStatus();
        if (cancelled || completedRef.current) return;

        apiFailRef.current = 0;
        setError(null);
        markPhaseDone(2);

        const boot = status?.boot_metrics || {};
        const gateError = boot.error || status?.system_state?.error;
        if (gateError) {
          setError(String(gateError));
          return;
        }

        const stage = String(boot.stage || "").toLowerCase();
        const isWarming = stage === "warming" || Boolean(boot.warming);
        if (isWarming) {
          markPhaseDone(3);
          applyWarmupPayload(setPercent, setLabel, boot.warming || boot);
        } else {
          const pct = Number(boot.percent);
          setPercent(Number.isFinite(pct) ? Math.min(99, Math.max(0, pct)) : 5);
          setLabel(boot.label || "Broker handshake in progress…");
        }

        if (isBackendBootComplete(boot)) {
          snapComplete();
          return;
        }
      } catch {
        if (cancelled) return;
        apiFailRef.current += 1;
        const elapsed = Date.now() - bootStartRef.current;
        if (elapsed >= API_GRACE_MS && apiFailRef.current >= MAX_API_FAILURES) {
          setError(`Cannot reach agent API — is the server running on :${resolveTargetPort()}?`);
        } else {
          setLabel(
            isLiveVanguardPort()
              ? `Waiting for Live Vanguard on :${resolveTargetPort()}…`
              : `Waiting for Shadow Simulator on :${resolveTargetPort()}…`,
          );
        }
      }

      if (!cancelled && !completedRef.current) {
        window.setTimeout(poll, POLL_MS);
      }
    };

    poll();
    return () => {
      cancelled = true;
    };
  }, [onComplete]);

  return (
    <div className="startup-splash startup-splash--monolith" role="status" aria-live="polite">
      <div className="startup-splash__card startup-splash__card--frameless">
        <p className="startup-splash__monolith-status">
          INITIALIZING APEX MONOLITH SYSTEM SHELL... STATUS: ACTIVE
        </p>

        <div className="startup-splash__phase-list">
          {BOOT_PHASES.map((phase) => {
            const done = completedPhases.has(phase.id) || activePhase > phase.id;
            const active = activePhase === phase.id && !done;
            return (
              <p
                key={phase.id}
                className={`startup-splash__phase-line${
                  done ? " startup-splash__phase-line--done" : ""
                }${active ? " startup-splash__phase-line--active" : ""}`}
              >
                {done ? "[OK]" : "[WIP]"} {phase.label.replace(/^\[WIP\] /, "")}
              </p>
            );
          })}
        </div>

        <p className="startup-splash__label">{label}</p>

        <div className="startup-splash__track" aria-hidden="true">
          <div
            className="startup-splash__bar startup-splash__bar--amber"
            style={{ width: `${percent}%`, transition: "width 0.25s ease-out" }}
          />
        </div>

        <p className="startup-splash__pct">{percent}%</p>

        {error && (
          <p className="startup-splash__error" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
