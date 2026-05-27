import { useState } from "react";
import { api } from "../api/client";

function barWidth(v, max = 100) {
  return `${Math.min(100, Math.max(0, (v / max) * 100))}%`;
}

function formatGateValue(gate) {
  const { name, value } = gate;
  if (value == null) return "—";
  if (typeof value !== "object") return String(value);

  if (name === "cold_start_gap") {
    const bars = value.bars ?? "—";
    const cold = value.cold ? "cold" : "ready";
    const gap = value.gap ? "gap" : "ok";
    return `${bars} bars · ${cold} · ${gap}`;
  }

  if (name === "signal_confidence") {
    const sig = value.signal;
    const conf =
      value.confidence ??
      (sig && typeof sig === "object" ? sig.confidence ?? sig.adjusted_confidence : null);
    const dir = sig && typeof sig === "object" ? sig.signal ?? sig.direction : null;
    const threshold = value.threshold;
    if (dir != null && conf != null && threshold != null) {
      return `${dir} ${Number(conf).toFixed(1)}% (need ${Number(threshold).toFixed(1)}%)`;
    }
    if (conf != null && threshold != null) {
      return `${Number(conf).toFixed(1)}% (need ${Number(threshold).toFixed(1)}%)`;
    }
  }

  try {
    return JSON.stringify(value);
  } catch {
    return "—";
  }
}

function GateCard({ gate, wide }) {
  const pass = gate.pass;
  const border = pass ? "border-l-green" : "border-l-red";
  return (
    <div className={`card border-l-4 ${border} ${wide ? "col-span-2" : ""}`}>
      <p className="label-caps">{gate.name?.replace(/_/g, " ")}</p>
      <p className="text-white mt-1">{formatGateValue(gate)}</p>
      <p className="text-muted text-[11px] mt-1">{gate.detail || (pass ? "Passing" : "Blocked")}</p>
    </div>
  );
}

export default function LiveTab({ tick }) {
  const [closeStep, setCloseStep] = useState(0);
  const [closing, setClosing] = useState(false);
  const health = tick?.health || {};
  const badge = health.badge || "WATCHING";
  const badgeColor =
    badge === "READY" ? "text-green" : badge === "BLOCKED" ? "text-red" : "text-amber";
  const gates = health.gates || [];
  const signal = tick?.signal || {};
  const pos = (tick?.positions || [])[0];

  const handleClose = async () => {
    if (!pos?.deal_id) return;
    if (closeStep < 1) {
      setCloseStep(1);
      return;
    }
    setClosing(true);
    try {
      await api.closeDeal(pos.deal_id);
      setCloseStep(0);
    } catch (e) {
      alert(e.message);
    } finally {
      setClosing(false);
    }
  };

  const entry = Number(pos?.entry) || 0;
  const current = Number(pos?.current) || entry;
  const stop = Number(pos?.stop) || entry;
  const target = Number(pos?.target) || entry;
  const range = target - stop || 1;
  const progress = ((current - stop) / range) * 100;

  return (
    <div className="p-4 space-y-4 max-w-5xl mx-auto">
      <div className="card text-center">
        <p className="label-caps">Master health</p>
        <p className={`price-lg mt-1 ${badgeColor}`}>{badge}</p>
        <p className="text-muted text-[11px] mt-2">{health.summary || "Awaiting engine data"}</p>
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="card">
          <p className="label-caps mb-2">Price</p>
          <div className="flex justify-between items-end">
            <div>
              <p className="text-muted text-[11px]">Bid</p>
              <p className="price-lg">{tick?.bid?.toFixed(1) ?? "—"}</p>
            </div>
            <div className="text-center">
              <p className="text-muted text-[11px]">Spread</p>
              <p className="text-amber font-medium">{tick?.spread?.toFixed(1) ?? "—"}</p>
            </div>
            <div className="text-right">
              <p className="text-muted text-[11px]">Offer</p>
              <p className="price-lg">{tick?.offer?.toFixed(1) ?? "—"}</p>
            </div>
          </div>
          <p className="text-muted text-[11px] mt-2">Tick age {tick?.tick_age_s ?? "—"}s</p>
        </div>

        <div className="card space-y-3">
          <p className="label-caps">Signal bars</p>
          {[
            ["Confidence", signal.confidence, "%"],
            ["Fitness", signal.fitness, ""],
            ["ATR", signal.atr, ""],
          ].map(([label, val]) => (
            <div key={label}>
              <div className="flex justify-between text-[11px] mb-1">
                <span className="text-muted">{label}</span>
                <span>
                  {val}
                  {label === "Confidence" ? "%" : ""}
                </span>
              </div>
              <div className="h-2 rounded bg-bg overflow-hidden">
                <div
                  className="h-full bg-blue"
                  style={{ width: barWidth(Number(val) || 0) }}
                />
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[
          ["Open P&L", pos?.pnl_gbp != null ? `£${Number(pos.pnl_gbp).toFixed(2)}` : "—"],
          ["Daily P&L", `£${Number(tick?.daily_pnl_gbp || 0).toFixed(2)}`],
          ["Win rate (20)", tick?.win_rate_20 != null ? `${tick.win_rate_20}%` : "—"],
          ["Balance", tick?.balance_gbp != null ? `£${Number(tick.balance_gbp).toFixed(2)}` : "—"],
        ].map(([k, v]) => (
          <div key={k} className="card">
            <p className="label-caps">{k}</p>
            <p className="price-lg mt-1">{v}</p>
          </div>
        ))}
      </div>

      <div>
        <p className="label-caps mb-2">Gates</p>
        <div className="grid grid-cols-2 gap-3">
          {gates.map((g) => (
            <GateCard key={g.name} gate={g} wide={g.name === "signal_confidence"} />
          ))}
        </div>
        <p className="text-muted text-[11px] mt-2">{health.summary || "No gate summary yet"}</p>
      </div>

      {pos && (
        <div className="card border border-border">
          <div className="flex justify-between items-start mb-4">
            <span
              className={`px-2 py-0.5 rounded text-[11px] font-medium ${
                pos.side === "BUY" ? "bg-green/20 text-green" : "bg-red/20 text-red"
              }`}
            >
              {pos.side}
            </span>
            <p className="price-lg text-green">
              {pos.pnl_gbp != null ? `£${Number(pos.pnl_gbp).toFixed(2)}` : "—"}
            </p>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
            {[
              ["Entry", pos.entry],
              ["Current", pos.current],
              ["Stop", pos.stop],
              ["Target", pos.target],
              ["Trail", pos.trail_active ? "Active" : "Off"],
              ["Break-even", pos.breakeven_hit ? "Hit" : "No"],
              ["To stop", pos.stop != null && pos.current != null ? (pos.current - pos.stop).toFixed(1) : "—"],
              ["To target", pos.target != null && pos.current != null ? (pos.target - pos.current).toFixed(1) : "—"],
            ].map(([k, v]) => (
              <div key={k}>
                <p className="label-caps">{k}</p>
                <p className="text-white">{v ?? "—"}</p>
              </div>
            ))}
          </div>
          <div className="mt-4 h-2 rounded bg-bg relative overflow-hidden">
            <div className="absolute left-0 top-0 h-full w-1 bg-red" />
            <div
              className="absolute top-0 h-full w-1 bg-green"
              style={{ left: `${Math.min(100, Math.max(0, progress))}%` }}
            />
          </div>
          <div className="border-t border-border mt-4 pt-4">
            <button
              type="button"
              disabled={closing}
              onClick={handleClose}
              className={`w-full py-2 rounded text-[13px] font-medium ${
                closeStep === 1
                  ? "bg-red text-white"
                  : "border border-red text-red hover:bg-red/10"
              }`}
            >
              {closeStep === 0
                ? "Close position now"
                : closing
                  ? "Closing…"
                  : "Confirm close — click again"}
            </button>
            {closeStep === 1 && (
              <button
                type="button"
                className="w-full mt-2 text-muted text-[11px]"
                onClick={() => setCloseStep(0)}
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
