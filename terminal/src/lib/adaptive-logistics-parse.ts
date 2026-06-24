import { EPIC_LABELS } from "@/lib/constants";
import type { FulfillmentPayload } from "@/lib/agent-client";

export type AdaptationEpicRow = {
  epic: string;
  label: string;
  regime: string;
  signalFloor: number;
  fitnessFloor: number;
  atrRatio: number;
};

export type BrokerOverrideLine = {
  id: string;
  raw: string;
  epic: string;
  timestamp: string;
};

export type StarvationTuneState = {
  state: string;
  starvation_risk: boolean;
  minutes_since_trade: number;
  minutes_to_starvation: number;
  decay_steps: number;
  auto_decay_enabled: boolean;
  ml_veto_override: number | null;
  alpha_seed_override: number | null;
  baseline_ml_floor: number;
  baseline_alpha_floor: number;
  effective_ml_floor: number;
  effective_alpha_floor: number;
  hard_floor_pct: number;
};

export type AdaptiveLogisticsSnapshot = {
  focusEpic: string;
  regime: string;
  regimeActive: boolean;
  effectiveGateFloor: number;
  baselineGateFloor: number;
  signalThreshold: number;
  fractionalMicroLot: boolean;
  microLotSize: number | null;
  epicRows: AdaptationEpicRow[];
  brokerOverrides: BrokerOverrideLine[];
  ticksCached: number;
  updatedAt: string;
  starvation: StarvationTuneState | null;
};

const V30_BASELINE_FLOOR = 55;
const V31_TARGET_FLOOR = 38;
const FOCUS_EPICS = [
  "CS.D.CFPGOLD.CFP.IP",
  "IX.D.DOW.IFM.IP",
  "IX.D.NASDAQ.IFM.IP",
] as const;

const DYNAMIC_ADAPT_RE =
  /\[DYNAMIC_ADAPT\]\s+epic=(\S+)\s+regime=(\S+)\s+sig=([\d.]+)\s+fit=([\d.]+)\s+atr_ratio=([\d.]+)/g;

const BROKER_OVERRIDE_RE =
  /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\|\s+\[BROKER_RULE_OVERRIDE\]\s+(.+?)\s+epic=(\S+)/;

export function parseDynamicAdaptLines(logText: string): AdaptationEpicRow[] {
  const rows = new Map<string, AdaptationEpicRow>();
  for (const match of logText.matchAll(DYNAMIC_ADAPT_RE)) {
    const epic = match[1];
    rows.set(epic, {
      epic,
      label: EPIC_LABELS[epic] ?? epic.split(".").pop() ?? epic,
      regime: match[2].toUpperCase(),
      signalFloor: Number(match[3]),
      fitnessFloor: Number(match[4]),
      atrRatio: Number(match[5]),
    });
  }
  return [...rows.values()];
}

export function parseBrokerOverrideLines(logText: string): BrokerOverrideLine[] {
  const out: BrokerOverrideLine[] = [];
  for (const line of logText.split("\n")) {
    const m = line.match(BROKER_OVERRIDE_RE);
    if (!m) continue;
    const raw = m[2].trim();
    out.push({
      id: `${m[1]}:${m[3]}:${raw.slice(0, 24)}`,
      timestamp: m[1],
      raw,
      epic: m[3],
    });
  }
  return out.slice(-24);
}

export function regimeLabel(regime: string): string {
  const key = regime.toLowerCase().replace(/\+.*/, "");
  if (key === "flat") return "FLAT REGIME";
  if (key === "hot") return "HOT REGIME";
  if (key === "sparse") return "SPARSE TAPE";
  if (key === "normal") return "NORMAL";
  return regime.toUpperCase();
}

export function buildAdaptiveLogisticsSnapshot(
  fulfillment: FulfillmentPayload | null,
  logText: string,
  focusEpic: string,
  starvation: StarvationTuneState | null = null,
): AdaptiveLogisticsSnapshot {
  const epicRows = parseDynamicAdaptLines(logText);
  const brokerOverrides = parseBrokerOverrideLines(logText);
  const byEpic = fulfillment?.gate_diagnostics?.by_epic ?? {};

  const mergedRows: AdaptationEpicRow[] = FOCUS_EPICS.map((epic) => {
    const fromLog = epicRows.find((r) => r.epic === epic);
    if (fromLog) return fromLog;

    const gateRow = byEpic[epic];
    const fitnessGate = (gateRow?.gates ?? []).find(
      (g) => g.name === "environment_fitness",
    );
    const fitnessVal =
      fitnessGate?.value && typeof fitnessGate.value === "object"
        ? Number(
            (fitnessGate.value as Record<string, unknown>).fitness_min ?? 0,
          )
        : 0;
    const tuningThr = Number(gateRow?.tuning?.signal_threshold ?? 0);
    const tuning = fulfillment?.tuning_variables as
      | Record<string, unknown>
      | undefined;

    return {
      epic,
      label: EPIC_LABELS[epic] ?? epic,
      regime: fitnessVal > 0 && fitnessVal <= V31_TARGET_FLOOR ? "FLAT" : "NORMAL",
      signalFloor: tuningThr || Number(tuning?.signal_threshold ?? 45),
      fitnessFloor: fitnessVal || V30_BASELINE_FLOOR,
      atrRatio: 1,
    };
  });

  const focusRow =
    mergedRows.find((r) => r.epic === focusEpic) ??
    epicRows.find((r) => r.epic === focusEpic) ??
    mergedRows[0];

  const regime = focusRow?.regime ?? "SPARSE";
  const effectiveGateFloor =
    starvation?.effective_alpha_floor ??
    focusRow?.fitnessFloor ??
    V30_BASELINE_FLOOR;
  const elasticActive = effectiveGateFloor <= V31_TARGET_FLOOR;

  const fractionalLine = brokerOverrides.find((b) =>
    /fractional|micro|0\.\d+|min_deal/i.test(b.raw),
  );
  const sizeMatch = fractionalLine?.raw.match(/from\s+([\d.]+)\s+to\s+([\d.]+)/i);
  const microLotSize = sizeMatch ? Number(sizeMatch[1]) : null;

  return {
    focusEpic,
    regime,
    regimeActive: regime.toLowerCase().startsWith("flat"),
    effectiveGateFloor: elasticActive ? effectiveGateFloor : V30_BASELINE_FLOOR,
    baselineGateFloor: V30_BASELINE_FLOOR,
    signalThreshold: focusRow?.signalFloor ?? 45,
    fractionalMicroLot:
      Boolean(fractionalLine) ||
      brokerOverrides.some((b) => /min_deal/i.test(b.raw) && /0\.\d+/.test(b.raw)),
    microLotSize,
    epicRows: mergedRows,
    brokerOverrides,
    ticksCached: Number(fulfillment?.ticks_cached ?? 0),
    updatedAt: fulfillment?.updated_at ?? new Date().toISOString(),
    starvation,
  };
}
