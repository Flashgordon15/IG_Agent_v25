function pillClass(color) {
  const map = {
    green: "border-green/40 text-green",
    red: "border-red/40 text-red",
    amber: "border-amber/40 text-amber",
    muted: "border-border text-muted",
  };
  return map[color] || map.muted;
}

function Dot({ color, pulse }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full bg-${color} ${pulse ? "pulse-open" : ""}`}
      style={{ backgroundColor: color === "green" ? "#3fb950" : color === "red" ? "#f85149" : "#d29922" }}
    />
  );
}

export default function StatusBar({ tick, reconnecting, onErrorsClick }) {
  const market = tick?.market_state || "OFFLINE";
  const stream = reconnecting ? "Reconnecting..." : tick?.stream_status || "DISCONNECTED";
  const rest = tick?.rest_calls_min ?? 0;
  const restColor = rest <= 4 ? "green" : rest === 5 ? "amber" : "red";
  const errCount = tick?.errors?.count ?? 0;
  const errType = tick?.errors?.type;
  const points = tick?.points?.state || "CAUTION";
  const pointsColor =
    points === "HEALTHY" ? "green" : points === "STOP" ? "red" : points === "WARNING" ? "amber" : "amber";
  const daily = tick?.daily_pnl_gbp ?? 0;
  const dailyColor = daily >= 0 ? "green" : "red";
  const syncAge = tick?.tick_age_s ?? null;
  const syncColor =
    syncAge === null ? "muted" : syncAge < 5 ? "green" : syncAge <= 30 ? "amber" : "red";
  const marketOpen = market === "OPEN";
  const streamLive = stream === "LIVE" && !reconnecting;

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-bg px-3 py-2 flex flex-wrap gap-2">
      <span className={`pill ${pillClass(marketOpen ? "green" : market === "STALE" ? "amber" : "muted")}`}>
        {marketOpen && <Dot color="green" pulse />}
        Japan 225 {market}
      </span>
      <span className={`pill ${pillClass(streamLive ? "green" : reconnecting ? "amber" : "red")}`}>
        Stream {stream}
      </span>
      <span className={`pill ${pillClass(restColor)}`}>REST {rest}/6</span>
      <button
        type="button"
        className={`pill ${pillClass(errCount ? "red" : "green")} cursor-pointer`}
        onClick={onErrorsClick}
      >
        ERRORS {errCount ? `${errType || "err"} (${errCount})` : "None"}
      </button>
      <span className={`pill ${pillClass(pointsColor)}`}>Points {points}</span>
      <span className={`pill ${pillClass(dailyColor)}`}>
        Daily P&amp;L {daily >= 0 ? "+" : ""}£{Number(daily).toFixed(2)}
      </span>
      <span className={`pill ${pillClass(syncColor)}`}>
        Sync {syncAge === null ? "—" : `${syncAge}s`} ago
      </span>
    </header>
  );
}
