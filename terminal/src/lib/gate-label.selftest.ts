/**
 * Gate label + chart series hygiene.
 * Run: npx --yes tsx src/lib/gate-label.selftest.ts
 * (from terminal/)
 */

import {
  CHART_EMPTY_WATERMARK,
  chartHasDrawableSeries,
  isValidMidSample,
  sanitizePriceSeries,
} from "./chart-series";
import {
  GATE_STANDBY_WAITING_BREAKOUT,
  mapGateVerdictLabel,
} from "./gate-label";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(`ASSERT FAIL: ${msg}`);
}

function main(): void {
  assert(
    mapGateVerdictLabel("GATE_UNKNOWN") === GATE_STANDBY_WAITING_BREAKOUT,
    "GATE_UNKNOWN → standby",
  );
  assert(
    mapGateVerdictLabel(null) === GATE_STANDBY_WAITING_BREAKOUT,
    "null → standby",
  );
  assert(
    mapGateVerdictLabel("") === GATE_STANDBY_WAITING_BREAKOUT,
    "empty → standby",
  );
  assert(
    mapGateVerdictLabel("unknown") === GATE_STANDBY_WAITING_BREAKOUT,
    "unknown → standby",
  );
  assert(
    mapGateVerdictLabel("MACRO_TREND_BLOCKING") === "MACRO_TREND_BLOCKING",
    "real veto preserved",
  );
  assert(
    mapGateVerdictLabel("spread_wide") === "SPREAD_WIDE",
    "normalize case/spaces",
  );
  assert(
    !mapGateVerdictLabel("GATE_UNKNOWN").includes("SETUP"),
    "must not invent SETUP",
  );

  const cleaned = sanitizePriceSeries([
    51900,
    Number.NaN,
    Number.POSITIVE_INFINITY,
    -1,
    0,
    null,
    undefined,
    51910.5,
  ]);
  assert(cleaned.length === 2, `sanitize length got ${cleaned.length}`);
  assert(cleaned[0] === 51900 && cleaned[1] === 51910.5, "sanitize values");
  assert(chartHasDrawableSeries(cleaned) === true, "drawable after clean");
  assert(chartHasDrawableSeries([51900]) === false, "single point not drawable");
  assert(chartHasDrawableSeries([Number.NaN, 0]) === false, "poison not drawable");
  assert(isValidMidSample(51900) === true, "valid mid");
  assert(isValidMidSample(Number.NaN) === false, "reject NaN mid");
  assert(isValidMidSample(Number.POSITIVE_INFINITY) === false, "reject Inf mid");
  assert(CHART_EMPTY_WATERMARK.includes("NO SERIES"), "watermark copy");

  console.log("gate-label.selftest: OK");
}

main();
