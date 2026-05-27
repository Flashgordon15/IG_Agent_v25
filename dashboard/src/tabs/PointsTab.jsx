const STATES = [
  { name: "HEALTHY", min: 10, mult: "1.0 / 0.5 / 0.25" },
  { name: "CAUTION", min: -5, mult: "0.5 / 0.25" },
  { name: "WARNING", min: -15, mult: "0.25 (92%+ only)" },
  { name: "STOP", min: -999, mult: "No trading" },
];

const CONF_TABLE = [
  ["HEALTHY", "≥92% → 1.0×", "85–91% → 0.5×", "80–84% → 0.25×"],
  ["CAUTION", "≥88% → 0.5×", "80–87% → 0.25×", "<80% → blocked"],
  ["WARNING", "≥92% → 0.25×", "other → blocked", ""],
  ["STOP", "All blocked", "", ""],
];

export default function PointsTab({ tick }) {
  const pts = tick?.points || {};
  const state = pts.state || "CAUTION";
  const cumulative = pts.cumulative ?? 0;

  const stateIdx = STATES.findIndex((s) => s.name === state);
  const position =
    state === "HEALTHY"
      ? Math.min(100, ((cumulative - 10) / 20) * 50 + 75)
      : state === "CAUTION"
        ? 50 + (cumulative / 15) * 25
        : state === "WARNING"
          ? 25
          : 5;

  return (
    <div className="p-4 max-w-3xl mx-auto space-y-6">
      <div className="grid grid-cols-3 gap-3">
        {[
          ["Points state", state],
          ["Last trade", `${pts.last_trade >= 0 ? "+" : ""}${pts.last_trade ?? 0} pts`],
          ["Session", `${pts.session >= 0 ? "+" : ""}${pts.session ?? 0} pts`],
        ].map(([k, v]) => (
          <div key={k} className="card">
            <p className="label-caps">{k}</p>
            <p className="price-lg mt-1">{v}</p>
          </div>
        ))}
      </div>

      <div className="card">
        <p className="label-caps mb-4">Threshold bands</p>
        {STATES.map((s, i) => (
          <div key={s.name} className="mb-3">
            <div className="flex justify-between text-[11px] mb-1">
              <span className={state === s.name ? "text-white font-medium" : "text-muted"}>
                {s.name}
              </span>
              <span className="text-muted">size {s.mult}</span>
            </div>
            <div className="h-2 rounded bg-bg overflow-hidden">
              <div
                className={`h-full ${state === s.name ? "bg-blue" : "bg-border"}`}
                style={{ width: state === s.name ? "100%" : "20%" }}
              />
            </div>
          </div>
        ))}
        <p className="text-muted text-[11px] mt-2">
          Cumulative {cumulative >= 0 ? "+" : ""}
          {cumulative} pts — marker at band {stateIdx + 1}/4
        </p>
        <div className="h-1 mt-2 rounded bg-border relative">
          <div className="absolute h-2 w-2 rounded-full bg-blue -top-0.5" style={{ left: `${position}%` }} />
        </div>
      </div>

      <div className="card overflow-x-auto">
        <p className="label-caps mb-2">Confidence × size by state</p>
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-muted text-left">
              <th className="pb-2">State</th>
              <th className="pb-2">High</th>
              <th className="pb-2">Standard</th>
              <th className="pb-2">Marginal</th>
            </tr>
          </thead>
          <tbody>
            {CONF_TABLE.map((row) => (
              <tr key={row[0]} className="border-t border-border">
                <td className="py-2 text-white">{row[0]}</td>
                <td className="py-2 text-muted">{row[1]}</td>
                <td className="py-2 text-muted">{row[2]}</td>
                <td className="py-2 text-muted">{row[3]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <p className="label-caps">Recovery</p>
        <p className="text-muted mt-2 text-[12px]">
          {state === "STOP"
            ? "Manual review required — STOP latched until cleared."
            : state === "WARNING"
              ? "Need cumulative above −5 pts or 3 recovery wins to return to CAUTION."
              : state === "CAUTION"
                ? "Need cumulative above +10 pts to reach HEALTHY."
                : "HEALTHY — full size bands available per confidence."}
        </p>
      </div>
    </div>
  );
}
