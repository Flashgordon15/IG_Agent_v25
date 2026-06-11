import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

/** Collapse consecutive identical reasons; keep latest timestamp and count. */
function collapseSignalLog(signals) {
  const out = [];
  for (const s of signals) {
    const prev = out[out.length - 1];
    if (prev && prev.reason === s.reason && prev.badge === s.badge) {
      prev.count = (prev.count || 1) + 1;
      prev.timestamp = s.timestamp;
    } else {
      out.push({ ...s, count: 1 });
    }
  }
  return out;
}

export default function TradesTab() {
  const [trades, setTrades] = useState([]);
  const [signals, setSignals] = useState([]);
  const [pointsTotal, setPointsTotal] = useState(0);

  useEffect(() => {
    api.trades().then((d) => {
      setTrades(d.trades || []);
      setPointsTotal(d.points_total || 0);
    });
    api.signals().then((d) => setSignals(d.signals || []));
    const id = setInterval(() => {
      api.trades().then((d) => {
        setTrades(d.trades || []);
        setPointsTotal(d.points_total || 0);
      });
      api.signals().then((d) => setSignals(d.signals || []));
    }, 15000);
    return () => clearInterval(id);
  }, []);

  const collapsedSignals = useMemo(() => collapseSignalLog(signals), [signals]);

  return (
    <div className="p-4 max-w-5xl mx-auto space-y-6">
      <div>
        <p className="label-caps mb-2">Closed trades</p>
        <div className="space-y-2">
          {trades.length === 0 && (
            <p className="text-muted card">No closed trades in journal yet.</p>
          )}
          {trades.map((t, i) => {
            const result = t.pending ? "OPEN" : t.result || "—";
            const pts = t.pnl_pts ?? 0;
            const ptsScore = t.points_score;
            return (
              <div key={i} className="card flex flex-wrap gap-3 items-center text-[12px]">
                <span
                  className={`px-2 py-0.5 rounded font-medium ${
                    t.direction === "BUY" ? "bg-green/20 text-green" : "bg-red/20 text-red"
                  }`}
                >
                  {t.direction}
                </span>
                <span className="text-white">{t.market}</span>
                <span className="text-muted">{t.closed_at}</span>
                <span>{t.confidence != null ? `${t.confidence}%` : ""}</span>
                <span className="text-muted">{t.setup}</span>
                <span>
                  {t.entry} → {t.exit}
                </span>
                <span className={pts >= 0 ? "text-green" : "text-red"}>
                  {pts >= 0 ? "+" : ""}
                  {pts} pts
                </span>
                <span>{t.pnl_gbp != null ? `£${Number(t.pnl_gbp).toFixed(2)}` : ""}</span>
                <span
                  className={
                    result === "WIN" ? "text-green" : result === "LOSS" ? "text-red" : "text-amber"
                  }
                >
                  {result}
                </span>
                {ptsScore != null && (
                  <span className={ptsScore >= 0 ? "text-green" : "text-red"}>
                    {ptsScore >= 0 ? "+" : ""}
                    {ptsScore} pts
                  </span>
                )}
              </div>
            );
          })}
        </div>
        <p className="text-right mt-3 text-[13px]">
          Running points total:{" "}
          <span className={pointsTotal >= 0 ? "text-green" : "text-red"}>
            {pointsTotal >= 0 ? "+" : ""}
            {pointsTotal.toFixed(1)} pts
          </span>
        </p>
      </div>

      <div>
        <p className="label-caps mb-2">Signal log (last 50)</p>
        <div className="space-y-1 max-h-96 overflow-y-auto">
          {collapsedSignals.map((s, i) => (
            <div key={i} className="card py-2 flex gap-3 text-[12px] items-center">
              <span className="text-muted shrink-0">{s.timestamp}</span>
              <span
                className={`px-1.5 rounded text-[10px] font-medium shrink-0 ${
                  s.badge === "TRADE" ? "bg-green/20 text-green" : "bg-amber/20 text-amber"
                }`}
              >
                {s.badge}
              </span>
              <span className="text-muted flex-1 min-w-0">
                {s.reason}
                {s.count > 1 ? (
                  <span className="text-white/80 ml-1.5">×{s.count}</span>
                ) : null}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
