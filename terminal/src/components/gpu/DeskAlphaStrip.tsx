"use client";

import { useEffect, useState } from "react";
import type { DeskCapitalView } from "@/hooks/useDeskCapital";
import { agentHttpBase } from "@/lib/agent-client";
import type {
  PositionAuthorityRow,
  QuantumSafetyMatrix,
  SniperMarketRow,
} from "@/lib/quantum-node-types";

type Props = {
  capital: DeskCapitalView;
  positions: PositionAuthorityRow[];
  scanner: SniperMarketRow[];
  safety: QuantumSafetyMatrix;
  unmonitored?: number;
  wsState: string;
};

type WhyIdle = {
  idle?: boolean;
  primary_blocker?: { id?: string; detail?: string } | null;
  self_questions?: string[];
  recommendation?: string;
};

function fmtGbp(v: number): string {
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
}

function missionEta(capital: DeskCapitalView): string {
  const remaining = capital.milestoneGbp - capital.provisionalCashGbp;
  if (remaining <= 0) return "MILESTONE HIT";
  if (capital.provisionalCashGbp <= 0 || !capital.sessionStartMs) {
    return "ETA — (no settled cash today)";
  }
  const elapsedH = Math.max(
    0.05,
    (Date.now() - capital.sessionStartMs) / 3_600_000,
  );
  const pace = capital.provisionalCashGbp / elapsedH; // £/hour
  if (pace <= 0.01) return "ETA — (pace ≤ 0)";
  const hours = remaining / pace;
  if (hours > 48) return `ETA >${hours.toFixed(0)}h @ £${pace.toFixed(1)}/h`;
  if (hours >= 1) return `ETA ${hours.toFixed(1)}h @ £${pace.toFixed(1)}/h`;
  return `ETA ${(hours * 60).toFixed(0)}m @ £${pace.toFixed(1)}/h`;
}

function riskBudget(positions: PositionAuthorityRow[]): {
  label: string;
  tone: string;
} {
  if (!positions.length) {
    return { label: "FLAT · £0 risk budget", tone: "" };
  }
  let softSum = 0;
  let upl = 0;
  for (const p of positions) {
    softSum += Math.abs(p.softLossGbp ?? 2.2);
    upl += p.pnlGbp ?? 0;
  }
  const room = softSum + Math.min(0, upl); // remaining pain room approx
  const left = Math.max(0, softSum - Math.max(0, -upl));
  return {
    label: `SOFT CAP £${softSum.toFixed(2)} · UPL ${fmtGbp(upl)} · ROOM £${left.toFixed(2)}`,
    tone: left < softSum * 0.35 ? "gpu-tone-amber" : "gpu-metric-val--emerald",
  };
}

function softProximity(positions: PositionAuthorityRow[]): {
  label: string;
  tone: string;
} {
  if (!positions.length) {
    return { label: "NO OPEN · PROX N/A", tone: "" };
  }
  let worstPct = 0;
  let worstDeal = "";
  for (const p of positions) {
    const soft = Math.abs(p.softLossGbp ?? 2.2);
    const pnl = p.pnlGbp ?? 0;
    if (soft <= 0) continue;
    // 0% = at entry, 100% = at soft loss
    const towardLoss = pnl < 0 ? Math.min(100, (Math.abs(pnl) / soft) * 100) : 0;
    if (towardLoss >= worstPct) {
      worstPct = towardLoss;
      worstDeal = p.dealId.slice(-6);
    }
  }
  const tone =
    worstPct >= 80
      ? "gpu-tone-loss"
      : worstPct >= 50
        ? "gpu-tone-amber"
        : "gpu-metric-val--emerald";
  return {
    label:
      worstPct <= 0
        ? "SOFT PROX CLEAR (all green)"
        : `WORST ${worstPct.toFixed(0)}% TO SOFT · …${worstDeal}`,
    tone,
  };
}

function sniperConversion(
  scanner: SniperMarketRow[],
  openCount: number,
): string {
  const armed = scanner.filter(
    (r) => r.statusKind === "long" || r.statusKind === "short",
  ).length;
  const proxy = scanner.filter((r) => r.statusKind === "proxy").length;
  const stack = scanner.filter((r) => r.inActiveStack).length;
  return `${armed} ARMED · ${stack} STACK · ${proxy} PROXY · ${openCount} OPEN`;
}

function deskScore(args: {
  capital: DeskCapitalView;
  positions: PositionAuthorityRow[];
  safety: QuantumSafetyMatrix;
  unmonitored: number;
  wsState: string;
}): { score: number; label: string; tone: string } {
  let score = 100;
  if (args.wsState !== "live") score -= 20;
  if (args.safety.driverIntegrity !== "OK") score -= 15;
  if (args.safety.thermalTrip || args.safety.macroBias === "VETO") score -= 25;
  if (args.unmonitored > 0) score -= Math.min(30, args.unmonitored * 15);
  const layersOk = args.positions.every((p) => p.layers.includes("G"));
  if (args.positions.length && !layersOk) score -= 15;
  const trailArmed = args.positions.filter(
    (p) => p.trailFloorGbp != null && p.trailFloorGbp > 0,
  ).length;
  if (args.positions.length) {
    const ratio = trailArmed / args.positions.length;
    if (ratio < 0.5) score -= 10;
  }
  score += Math.min(10, args.capital.progressPct / 10);
  score = Math.max(0, Math.min(100, Math.round(score)));
  const label =
    score >= 80 ? "PUSH" : score >= 55 ? "HOLD / MANAGE" : "PAUSE / DEFEND";
  const tone =
    score >= 80
      ? "gpu-metric-val--emerald"
      : score >= 55
        ? "gpu-tone-amber"
        : "gpu-tone-loss";
  return { score, label, tone };
}

export function DeskAlphaStrip({
  capital,
  positions,
  scanner,
  safety,
  unmonitored = 0,
  wsState,
}: Props) {
  const [why, setWhy] = useState<WhyIdle | null>(null);
  const eta = missionEta(capital);
  const risk = riskBudget(positions);
  const prox = softProximity(positions);
  const conv = sniperConversion(scanner, positions.length);
  const desk = deskScore({
    capital,
    positions,
    safety,
    unmonitored,
    wsState,
  });

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const r = await fetch(`${agentHttpBase()}/api/desk/why_idle?heal=true`, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!r.ok) return;
        const body = (await r.json()) as {
          after?: WhyIdle;
          before?: WhyIdle;
        };
        if (!alive) return;
        setWhy(body.after || body.before || null);
      } catch {
        /* non-fatal */
      }
    };
    void pull();
    const id = window.setInterval(pull, 15_000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const blocker =
    why?.primary_blocker?.id != null
      ? `${why.primary_blocker.id}: ${why.primary_blocker.detail || ""}`
      : safety.driverIntegrity !== "OK"
        ? `feed: ${safety.feedLabel}`
        : null;
  const question = why?.self_questions?.[0] || null;

  return (
    <section className="desk-alpha-strip" aria-label="Desk alpha mission strip">
      <div className="desk-alpha-cell">
        <span className="gpu-metric-key">MISSION ETA</span>
        <strong className="desk-alpha-value">{eta}</strong>
        <span className="gpu-capital-sub">
          provisional {fmtGbp(capital.provisionalCashGbp)} / £
          {capital.milestoneGbp.toFixed(0)}
        </span>
      </div>
      <div className="desk-alpha-cell">
        <span className="gpu-metric-key">RISK BUDGET</span>
        <strong className={`desk-alpha-value desk-alpha-value--sm ${risk.tone}`}>
          {risk.label}
        </strong>
      </div>
      <div className="desk-alpha-cell">
        <span className="gpu-metric-key">SOFT-LOSS PROXIMITY</span>
        <strong className={`desk-alpha-value desk-alpha-value--sm ${prox.tone}`}>
          {prox.label}
        </strong>
      </div>
      <div className="desk-alpha-cell">
        <span className="gpu-metric-key">SNIPER CONVERSION</span>
        <strong className="desk-alpha-value desk-alpha-value--sm">{conv}</strong>
      </div>
      <div className="desk-alpha-cell desk-alpha-cell--score">
        <span className="gpu-metric-key">DESK SCORE</span>
        <strong className={`desk-alpha-score ${desk.tone}`}>{desk.score}</strong>
        <span className={`gpu-capital-sub ${desk.tone}`}>{desk.label}</span>
        <span
          className="desk-alpha-backlog"
          title={question || blocker || "Self-assessment"}
        >
          {blocker
            ? `AI WHY IDLE · ${blocker.slice(0, 96)}`
            : "AI WHY IDLE · entries path clear"}
        </span>
      </div>
    </section>
  );
}
