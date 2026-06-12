import { memo, useEffect, useMemo, useState } from "react";

const LONDON_TZ = "Europe/London";
const WEEKEND_REOPEN_LABEL = "Sunday 22:00 BST";
const CLOSED_WARNING =
  "Prices frozen until Sunday 22:00 BST. Active positions locked by exchange.";

/** Earliest session_open.next_open from backend market slices, if present. */
export function earliestNextOpenIso(markets) {
  if (!markets || typeof markets !== "object") return null;
  let best = null;
  for (const m of Object.values(markets)) {
    const gates = m?.health?.gates;
    if (!Array.isArray(gates)) continue;
    const session = gates.find((g) => g.name === "session_open");
    const iso =
      session && typeof session.value === "object" ? session.value?.next_open : null;
    if (!iso) continue;
    const t = Date.parse(iso);
    if (Number.isNaN(t)) continue;
    if (best == null || t < best) best = t;
  }
  return best != null ? new Date(best).toISOString() : null;
}

/** Institutional FX / index weekend reopen — next Sun 22:00 Europe/London. */
export function nextSunday2200London(from = new Date()) {
  const startMs = from.getTime();
  const endMs = startMs + 8 * 86400000;
  for (let ms = startMs; ms <= endMs; ms += 60000) {
    const d = new Date(ms);
    const weekday = d.toLocaleDateString("en-GB", { timeZone: LONDON_TZ, weekday: "short" });
    const hm = d.toLocaleTimeString("en-GB", {
      timeZone: LONDON_TZ,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    if (weekday === "Sun" && hm === "22:00") return d;
  }
  return new Date(startMs + 2 * 86400000);
}

export function resolveReopenAt({ markets, now = new Date() }) {
  const backendIso = earliestNextOpenIso(markets);
  if (backendIso) {
    const backendAt = new Date(backendIso);
    if (backendAt.getTime() > now.getTime()) return backendAt;
  }
  return nextSunday2200London(now);
}

export function countMarketsOpen(markets) {
  if (!markets || typeof markets !== "object") return null;
  const states = Object.values(markets).map((m) =>
    String(m?.market_state ?? "").toUpperCase(),
  );
  if (!states.length) return null;
  return states.filter((s) => s === "OPEN").length;
}

export function isSessionClosed({ marketState, markets, marketsOpenCount }) {
  const ms = String(marketState ?? "").toUpperCase();
  if (ms === "CLOSED" || ms === "OFFLINE") return true;
  if (typeof marketsOpenCount === "number" && marketsOpenCount === 0) return true;
  const open = countMarketsOpen(markets);
  if (open === 0) return true;
  return false;
}

function formatCountdown(target, now) {
  const ms = target.getTime() - now.getTime();
  if (ms <= 0) return "imminent";
  const totalMin = Math.floor(ms / 60000);
  const days = Math.floor(totalMin / (60 * 24));
  const hours = Math.floor((totalMin % (60 * 24)) / 60);
  const mins = totalMin % 60;
  if (days > 0) return `${days}d ${hours}h ${mins}m`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

function formatReopenClock(target) {
  try {
    const day = target.toLocaleDateString("en-GB", {
      timeZone: LONDON_TZ,
      weekday: "short",
    });
    const time = target.toLocaleTimeString("en-GB", {
      timeZone: LONDON_TZ,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    return `${day} ${time} London`;
  } catch {
    return WEEKEND_REOPEN_LABEL;
  }
}

export function buildMarketStatusTimerProps(rawState, epic) {
  const markets = rawState?.markets;
  const openFromMarkets = markets
    ? Object.values(markets).filter(
        (m) => String(m?.market_state ?? "").toUpperCase() === "OPEN",
      ).length
    : null;
  return {
    marketState:
      markets?.[epic]?.market_state
      ?? rawState?.market_state
      ?? (openFromMarkets === 0 ? "CLOSED" : null),
    markets,
    marketsOpenCount:
      typeof rawState?.markets_open_count === "number"
        ? rawState.markets_open_count
        : openFromMarkets,
  };
}

export function marketStatusTimerPropsEqual(prev, next) {
  if (prev.marketState !== next.marketState) return false;
  if (prev.marketsOpenCount !== next.marketsOpenCount) return false;
  if (prev.variant !== next.variant) return false;
  return (
    earliestNextOpenIso(prev.markets) === earliestNextOpenIso(next.markets)
    && countMarketsOpen(prev.markets) === countMarketsOpen(next.markets)
  );
}

function MarketStatusTimer({
  marketState,
  markets,
  marketsOpenCount = null,
  variant = "banner",
}) {
  const closed = isSessionClosed({ marketState, markets, marketsOpenCount });
  const reopenAt = useMemo(
    () => (closed ? resolveReopenAt({ markets }) : null),
    [closed, markets],
  );
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    if (!closed || !reopenAt) return undefined;
    const id = setInterval(() => setNow(new Date()), 30000);
    return () => clearInterval(id);
  }, [closed, reopenAt]);

  if (!closed) return null;

  const countdown = reopenAt ? formatCountdown(reopenAt, now) : "—";
  const reopenClock = reopenAt ? formatReopenClock(reopenAt) : WEEKEND_REOPEN_LABEL;

  if (variant === "inline") {
    return (
      <div
        className="hidden min-w-0 max-w-[280px] flex-col gap-0.5 sm:flex"
        title={CLOSED_WARNING}
      >
        <span className="truncate text-[10px] tabular-nums text-muted/60">
          Reopens {reopenClock} · {countdown}
        </span>
        <span className="truncate text-[9px] leading-snug text-muted/45">
          {CLOSED_WARNING}
        </span>
      </div>
    );
  }

  return (
    <div
      className="rounded-lg border border-border/60 bg-card/40 px-3 py-2"
      role="status"
      aria-live="polite"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-muted">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-border" aria-hidden />
          Closed
        </span>
        <span className="text-[11px] tabular-nums text-muted/70">
          Reopens {reopenClock}
          <span className="mx-1.5 text-muted/40" aria-hidden>
            ·
          </span>
          <span className="font-medium text-muted/80">{countdown}</span>
        </span>
      </div>
      <p className="mt-1 text-[10px] leading-snug text-muted/50">{CLOSED_WARNING}</p>
    </div>
  );
}

export default memo(MarketStatusTimer, marketStatusTimerPropsEqual);
