"use client";

import { useCallback, useState } from "react";
import { useAdaptiveLogistics } from "@/hooks/useAdaptiveLogistics";
import { regimeLabel } from "@/lib/adaptive-logistics-parse";
import { agentHttpBase, postAgentTune } from "@/lib/agent-client";

type Props = {
  focusEpic: string;
  fault?: boolean;
};

function RegimeBadge({
  regime,
  active,
}: {
  regime: string;
  active: boolean;
}) {
  const label = regimeLabel(regime);
  const flat = active || label === "FLAT REGIME";
  return (
    <span
      className={`inline-flex items-center rounded-sm border px-2 py-1 text-[10px] font-mono font-semibold tracking-wider ${
        flat
          ? "border-amber-400/60 bg-amber-500/15 text-amber-300 shadow-[0_0_18px_rgba(251,191,36,0.25)]"
          : regime.toLowerCase().includes("hot")
            ? "border-rose-500/50 bg-rose-950/40 text-rose-300"
            : "border-zinc-600 bg-zinc-900/80 text-zinc-400"
      }`}
    >
      {label}
    </span>
  );
}

function GateDial({
  effective,
  baseline,
  signal,
}: {
  effective: number;
  baseline: number;
  signal: number;
}) {
  const elastic = effective <= 38;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-end gap-3">
        <span
          className={`cq-mono text-4xl font-bold leading-none ${
            elastic ? "text-emerald-400" : "text-slate-300"
          }`}
        >
          {effective.toFixed(0)}%
        </span>
        <span className="cq-mono text-[10px] text-zinc-500 line-through">
          v30 {baseline}%
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-zinc-800">
        <div
          className={`h-full transition-all duration-500 ${
            elastic ? "bg-gradient-to-r from-emerald-600 to-emerald-400" : "bg-slate-500"
          }`}
          style={{ width: `${Math.min(100, effective)}%` }}
        />
      </div>
      <p className="cq-mono text-[9px] text-zinc-500">
        Effective Gate Pass Floor · signal {signal.toFixed(0)}%
      </p>
    </div>
  );
}

function OverrideTicker({ lines }: { lines: { id: string; raw: string; epic: string }[] }) {
  const text =
    lines.length > 0
      ? lines
          .map((l) => `[${l.epic.split(".").pop()}] ${l.raw}`)
          .join("   ◆   ")
      : "Awaiting [BROKER_RULE_OVERRIDE] telemetry from sandbox engine…";

  return (
    <div className="relative mt-2 overflow-hidden border-t border-zinc-800/80 bg-zinc-950/90 py-1">
      <div className="pointer-events-none absolute inset-y-0 left-0 z-10 w-8 bg-gradient-to-r from-zinc-950 to-transparent" />
      <div className="pointer-events-none absolute inset-y-0 right-0 z-10 w-8 bg-gradient-to-l from-zinc-950 to-transparent" />
      <div className="flex whitespace-nowrap">
        <p className="cq-mono cq-marquee px-2 text-[9px] text-amber-200/90">
          {text}
          {lines.length > 0 ? `   ◆   ${text}` : ""}
        </p>
      </div>
    </div>
  );
}

function StarvationClock({
  minutesSince,
  minutesToStarvation,
  state,
}: {
  minutesSince: number;
  minutesToStarvation: number;
  state: string;
}) {
  const starved = state === "STATE_STARVED";
  const risk = minutesToStarvation <= 1 || starved;
  const label = starved
    ? `STARVED · ${Math.floor(minutesSince)}m idle`
    : risk
      ? `STARVATION RISK: ${Math.ceil(minutesToStarvation)}m to decay`
      : `${Math.floor(minutesSince)}m since last trade`;

  return (
    <span
      className={`inline-flex items-center rounded-sm border px-2 py-1 text-[10px] font-mono font-semibold tracking-wider ${
        starved
          ? "border-rose-500/70 bg-rose-950/50 text-rose-300 shadow-[0_0_14px_rgba(244,63,94,0.35)]"
          : risk
            ? "border-amber-400/60 bg-amber-500/10 text-amber-300"
            : "border-emerald-600/40 bg-emerald-950/30 text-emerald-400"
      }`}
    >
      {label}
    </span>
  );
}

function FatigueControls({
  mlFloor,
  alphaFloor,
  autoDecay,
  hardFloor,
  onTune,
}: {
  mlFloor: number;
  alphaFloor: number;
  autoDecay: boolean;
  hardFloor: number;
  onTune: (body: {
    ml_veto_override?: number;
    alpha_seed_override?: number;
    auto_decay_enabled?: boolean;
  }) => void;
}) {
  const [busy, setBusy] = useState(false);

  const send = useCallback(
    async (body: {
      ml_veto_override?: number;
      alpha_seed_override?: number;
      auto_decay_enabled?: boolean;
    }) => {
      setBusy(true);
      try {
        await postAgentTune(body);
        onTune(body);
      } finally {
        setBusy(false);
      }
    },
    [onTune],
  );

  return (
    <div className="space-y-3">
      <label className="flex items-center justify-between gap-2 text-[9px] font-mono text-zinc-400">
        <span>Auto-Decay (15m / −2.5pt)</span>
        <button
          type="button"
          disabled={busy}
          onClick={() => void send({ auto_decay_enabled: !autoDecay })}
          className={`relative h-5 w-9 rounded-full transition-colors ${
            autoDecay ? "bg-emerald-600" : "bg-zinc-700"
          }`}
        >
          <span
            className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform ${
              autoDecay ? "left-4" : "left-0.5"
            }`}
          />
        </button>
      </label>

      <div>
        <div className="mb-1 flex justify-between text-[9px] font-mono text-zinc-500">
          <span>ML Veto Floor</span>
          <span className="text-slate-300">{mlFloor.toFixed(1)}%</span>
        </div>
        <input
          type="range"
          min={hardFloor}
          max={55}
          step={0.5}
          value={mlFloor}
          disabled={busy}
          onChange={(e) =>
            void send({ ml_veto_override: Number(e.target.value) })
          }
          className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-zinc-800 accent-emerald-500"
        />
      </div>

      <div>
        <div className="mb-1 flex justify-between text-[9px] font-mono text-zinc-500">
          <span>Alpha Seed Floor</span>
          <span className="text-slate-300">{alphaFloor.toFixed(1)}%</span>
        </div>
        <input
          type="range"
          min={hardFloor}
          max={55}
          step={0.5}
          value={alphaFloor}
          disabled={busy}
          onChange={(e) =>
            void send({ alpha_seed_override: Number(e.target.value) })
          }
          className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-zinc-800 accent-amber-500"
        />
      </div>
    </div>
  );
}

export function AdaptiveLogisticsPanel({ focusEpic, fault }: Props) {
  const { snapshot, error, refresh } = useAdaptiveLogistics(focusEpic);

  const regime = snapshot?.regime ?? "SPARSE";
  const elastic = (snapshot?.effectiveGateFloor ?? 55) <= 38;
  const starvation = snapshot?.starvation;
  const mlFloor =
    starvation?.ml_veto_override ??
    starvation?.effective_ml_floor ??
    snapshot?.signalThreshold ??
    45;
  const alphaFloor =
    starvation?.alpha_seed_override ??
    starvation?.effective_alpha_floor ??
    snapshot?.effectiveGateFloor ??
    38;

  return (
    <section className="cq-panel flex min-h-0 flex-col border-zinc-700/60 bg-gradient-to-b from-zinc-950 to-[#0a0a0f] shadow-lg shadow-black/40">
      <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-1.5">
        <div className="flex items-center gap-2">
          <span className="cq-label text-zinc-400">Adaptive Logistics</span>
          <span className="rounded bg-emerald-950/60 px-1.5 py-0.5 text-[8px] font-mono text-emerald-400">
            v31 SANDBOX
          </span>
          {starvation ? (
            <StarvationClock
              minutesSince={starvation.minutes_since_trade}
              minutesToStarvation={starvation.minutes_to_starvation}
              state={starvation.state}
            />
          ) : null}
        </div>
        <div className="flex items-center gap-3">
          <span className="cq-mono text-[9px] text-zinc-500">
            {snapshot?.ticksCached?.toLocaleString() ?? "—"} ticks RAM
          </span>
          <span
            className={`cq-mono text-[9px] ${
              fault || error ? "text-rose-400" : "text-emerald-500"
            }`}
          >
            {error ? "FEED:DEG" : fault ? "STALE" : "LIVE"}
          </span>
          <span className="cq-mono text-[9px] text-zinc-600">{agentHttpBase()}</span>
        </div>
      </header>

      <div className="grid grid-cols-4 gap-px bg-zinc-800/50 p-px">
        <article className="bg-zinc-950/90 p-3">
          <h3 className="mb-2 text-[9px] font-mono uppercase tracking-widest text-zinc-500">
            Regime Capture
          </h3>
          <RegimeBadge regime={regime} active={snapshot?.regimeActive ?? false} />
          <ul className="mt-3 space-y-1">
            {(snapshot?.epicRows ?? []).map((row) => (
              <li
                key={row.epic}
                className="flex items-center justify-between text-[9px] font-mono text-zinc-500"
              >
                <span className={row.epic === focusEpic ? "text-amber-200" : ""}>
                  {row.label}
                </span>
                <span
                  className={
                    row.regime.toLowerCase().startsWith("flat")
                      ? "text-amber-300"
                      : "text-zinc-600"
                  }
                >
                  {row.fitnessFloor.toFixed(0)}%
                </span>
              </li>
            ))}
          </ul>
        </article>

        <article className="bg-zinc-950/90 p-3">
          <h3 className="mb-2 text-[9px] font-mono uppercase tracking-widest text-zinc-500">
            Gate Elasticity Dial
          </h3>
          <GateDial
            effective={snapshot?.effectiveGateFloor ?? 55}
            baseline={snapshot?.baselineGateFloor ?? 55}
            signal={snapshot?.signalThreshold ?? 45}
          />
          <p
            className={`mt-2 text-[9px] font-mono ${
              elastic ? "text-emerald-400/90" : "text-zinc-600"
            }`}
          >
            {elastic
              ? "38% elastic floor engaged — look-back adaptation active"
              : "Holding v30 baseline until tape + regime qualify"}
          </p>
        </article>

        <article className="bg-zinc-950/90 p-3">
          <h3 className="mb-2 text-[9px] font-mono uppercase tracking-widest text-zinc-500">
            Contract Sizing Monitor
          </h3>
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                snapshot?.fractionalMicroLot ? "bg-emerald-400 shadow-[0_0_8px_#34d399]" : "bg-zinc-600"
              }`}
            />
            <span className="cq-mono text-sm text-slate-200">
              {snapshot?.fractionalMicroLot
                ? "Fractional micro-lots UNCLIPPED"
                : "Integer lot path (broker min active)"}
            </span>
          </div>
          <p className="mt-2 cq-mono text-[10px] text-zinc-500">
            {snapshot?.microLotSize != null
              ? `Last observed size ${snapshot.microLotSize} → broker-compliant dispatch`
              : "0.25 / sub-1.0 sizes pass BrokerRulebookGuard without floor_contract_size"}
          </p>
          <p className="mt-1 cq-mono text-[9px] text-emerald-600/80">
            min_deal &lt; 1.0 → skip integer flooring
          </p>
        </article>

        <article className="bg-zinc-950/90 p-3">
          <h3 className="mb-2 text-[9px] font-mono uppercase tracking-widest text-zinc-500">
            Fatigue Relaxation
          </h3>
          <FatigueControls
            mlFloor={mlFloor}
            alphaFloor={alphaFloor}
            autoDecay={starvation?.auto_decay_enabled ?? true}
            hardFloor={starvation?.hard_floor_pct ?? 35}
            onTune={() => refresh?.()}
          />
          <p className="mt-2 cq-mono text-[9px] text-zinc-600">
            decay steps: {starvation?.decay_steps ?? 0} · state{" "}
            {starvation?.state ?? "NORMAL"}
          </p>
        </article>
      </div>

      <OverrideTicker lines={snapshot?.brokerOverrides ?? []} />
    </section>
  );
}
