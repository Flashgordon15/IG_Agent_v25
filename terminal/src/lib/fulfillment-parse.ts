import { EPIC_LABELS } from "@/lib/constants";
import type {
  FulfillmentPayload,
  GateDiagnosticRow,
  FrontierEpicRow,
  MarketQuote,
} from "@/lib/agent-client";

export type WatchlistRow = {
  epic: string;
  ticker: string;
  price: number;
  bid: number;
  offer: number;
  delta24h: number;
  deltaPct: number;
  direction: "UP" | "DOWN" | "FLAT";
  history: number[];
  gatingReason?: string;
};

export type DepthLevel = {
  label: string;
  weight: number;
  pct: number;
};

export type LedgerRow = {
  timestamp: string;
  side: "BUY" | "SELL";
  size: number;
  venue: string;
  epic: string;
};

function gateValue(row: GateDiagnosticRow, name: string): Record<string, unknown> | null {
  for (const g of row.gates ?? []) {
    if (g.name === name && g.value && typeof g.value === "object") {
      return g.value as Record<string, unknown>;
    }
  }
  return null;
}

export function epicRowsFromFulfillment(
  payload: FulfillmentPayload | null,
): Record<string, GateDiagnosticRow> {
  if (!payload) return {};
  const gd = payload.gate_diagnostics?.by_epic;
  if (gd && typeof gd === "object") return gd;
  return {};
}

export function frontierFromFulfillment(
  payload: FulfillmentPayload | null,
): Record<string, FrontierEpicRow> {
  if (!payload) return {};
  return payload.alpha_frontier_tracker?.by_epic ?? {};
}

export function liveQuoteForEpic(
  payload: FulfillmentPayload | null,
  epic: string,
): MarketQuote | null {
  if (!payload) return null;
  const direct = payload.market_quotes?.[epic];
  if (direct && (direct.mid > 0 || direct.bid > 0 || direct.offer > 0)) {
    return direct;
  }
  for (const row of payload.market_quotes_list ?? []) {
    if (row.epic === epic) return row;
  }
  return null;
}

function midFromQuote(q: MarketQuote | null): number {
  if (!q) return 0;
  if (q.mid > 0) return q.mid;
  if (q.last_price > 0) return q.last_price;
  if (q.bid > 0 && q.offer > 0) return (q.bid + q.offer) / 2;
  return q.bid > 0 ? q.bid : q.offer;
}

export function buildWatchlistRows(
  payload: FulfillmentPayload | null,
  priceHistory: Record<string, number[]>,
): WatchlistRow[] {
  const gateByEpic = epicRowsFromFulfillment(payload);
  const frontier = frontierFromFulfillment(payload);
  const quoteEpics = Object.keys(payload?.market_quotes ?? {});
  const epics = Object.keys(gateByEpic).length
    ? Object.keys(gateByEpic)
    : quoteEpics.length
      ? quoteEpics
      : Object.keys(frontier);

  return epics.map((epic) => {
    const gate = gateByEpic[epic];
    const ft = frontier[epic];
    const quote = liveQuoteForEpic(payload, epic);
    const price = midFromQuote(quote);
    const bid = quote?.bid ?? price;
    const offer = quote?.offer ?? price;
    const history = priceHistory[epic] ?? (price > 0 ? [price] : []);
    const open = history[0] ?? price;
    const delta24h = price > 0 && open > 0 ? price - open : 0;
    const deltaPct = open !== 0 ? (delta24h / open) * 100 : 0;
    let direction: WatchlistRow["direction"] = "FLAT";
    if (delta24h > 0.0001) direction = "UP";
    else if (delta24h < -0.0001) direction = "DOWN";

    return {
      epic,
      ticker: EPIC_LABELS[epic] ?? epic.split(".").pop() ?? epic,
      price,
      bid,
      offer,
      delta24h,
      deltaPct,
      direction,
      history: history.length > 1 ? history : price > 0 ? [...history, price] : history,
      gatingReason: gate?.gating_reason,
    };
  });
}

export function buildOrderBookDepth(
  payload: FulfillmentPayload | null,
  focusEpic: string,
): { asks: DepthLevel[]; bids: DepthLevel[] } {
  const gate = epicRowsFromFulfillment(payload)[focusEpic];
  if (!gate) {
    return { asks: [], bids: [] };
  }
  const asks: DepthLevel[] = [];
  const bids: DepthLevel[] = [];
  for (const g of gate.gates ?? []) {
    const val = g.value;
    const weight =
      typeof val === "object" && val !== null
        ? Number(
            (val as Record<string, unknown>).confidence ??
              (val as Record<string, unknown>).score ??
              (val as Record<string, unknown>).ml_probability ??
              0,
          )
        : 0;
    const w = Math.max(1, weight || (g.passed ? 55 : 30));
    const label = g.name.replace(/_/g, " ");
    if (g.passed) {
      bids.push({ label, weight: w, pct: 0 });
    } else {
      asks.push({ label, weight: w, pct: 0 });
    }
  }
  const askMax = Math.max(...asks.map((a) => a.weight), 1);
  const bidMax = Math.max(...bids.map((b) => b.weight), 1);
  asks.forEach((a) => {
    a.pct = Math.round((a.weight / askMax) * 100);
  });
  bids.forEach((b) => {
    b.pct = Math.round((b.weight / bidMax) * 100);
  });
  return { asks: asks.reverse(), bids };
}

export function buildCorrelationMatrix(
  payload: FulfillmentPayload | null,
): { labels: string[]; matrix: number[][] } {
  const frontier = frontierFromFulfillment(payload);
  const labels = ["GOLD", "WALL ST", "NIKKEI", "EUR/USD"];
  const epics = [
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
  ];
  const vectors = epics.map((epic) => {
    const v = frontier[epic]?.vector;
    return [
      Number(v?.momentum ?? 0),
      Number(v?.rsi ?? 50) / 100,
      Number(v?.atr ?? 0),
    ];
  });

  const matrix: number[][] = [];
  for (let i = 0; i < epics.length; i++) {
    const row: number[] = [];
    for (let j = 0; j < epics.length; j++) {
      if (i === j) {
        row.push(1);
        continue;
      }
      const a = vectors[i];
      const b = vectors[j];
      const dot = a.reduce((s, v, k) => s + v * (b[k] ?? 0), 0);
      const magA = Math.sqrt(a.reduce((s, v) => s + v * v, 0)) || 1;
      const magB = Math.sqrt(b.reduce((s, v) => s + v * v, 0)) || 1;
      const coeff = dot / (magA * magB);
      row.push(Math.max(-1, Math.min(1, coeff)));
    }
    matrix.push(row);
  }
  return { labels, matrix };
}

export function buildLedgerRows(
  payload: FulfillmentPayload | null,
  triageRows: Array<Record<string, unknown>>,
  tradeRows: Array<Record<string, unknown>>,
): LedgerRow[] {
  const out: LedgerRow[] = [];

  for (const row of payload?.performance_rows ?? []) {
    const ts = String(row.executed_at ?? row.closed_at ?? "");
    const side = String(row.direction ?? row.action ?? "BUY").toUpperCase() as
      | "BUY"
      | "SELL";
    out.push({
      timestamp: formatLedgerTs(ts),
      side: side === "SELL" ? "SELL" : "BUY",
      size: Number(row.size ?? 1),
      venue: String(row.source ?? "IG_OTC").replace(/ig_rest_/i, "IG_").toUpperCase(),
      epic: String(row.epic ?? "—"),
    });
  }

  for (const row of triageRows) {
    const tsNum = Number(row.ts ?? 0);
    const d = tsNum > 1e12 ? new Date(tsNum) : new Date(tsNum * 1000);
    out.push({
      timestamp: formatLedgerTs(d.toISOString()),
      side: "BUY",
      size: Number(row.size ?? 1),
      venue: "IG_OTC",
      epic: String(row.epic ?? "—"),
    });
  }

  for (const row of tradeRows) {
    const side = String(row.direction ?? "BUY").toUpperCase() as "BUY" | "SELL";
    out.push({
      timestamp: formatLedgerTs(String(row.closed_at ?? "")),
      side: side === "SELL" ? "SELL" : "BUY",
      size: 1,
      venue: "IG_OTC",
      epic: String(row.market ?? row.setup ?? "—"),
    });
  }

  return out
    .filter((r) => r.timestamp.length > 0)
    .slice(0, 128);
}

function formatLedgerTs(raw: string): string {
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) {
    if (/^\d{2}:\d{2}:\d{2}/.test(raw)) return raw;
    return "";
  }
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(6, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

export function candleBarsFromHistory(history: number[]): Array<{
  o: number;
  h: number;
  l: number;
  c: number;
}> {
  if (history.length < 2) {
    const p = history[0] ?? 0;
    return [{ o: p, h: p, l: p, c: p }];
  }
  const bars: Array<{ o: number; h: number; l: number; c: number }> = [];
  const chunk = Math.max(1, Math.floor(history.length / 24));
  for (let i = 0; i < history.length; i += chunk) {
    const slice = history.slice(i, i + chunk);
    if (!slice.length) continue;
    const o = slice[0];
    const c = slice[slice.length - 1];
    bars.push({ o, h: Math.max(...slice), l: Math.min(...slice), c });
  }
  return bars.slice(-32);
}
