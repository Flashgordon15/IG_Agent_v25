"use client";

/**
 * v36 — per-account REST token bucket drain/refill (CFD 40/s · SB 10/s).
 * Polls /api/desk/rest_budget on each engine port; falls back to rate-cap meters.
 */

import { useEffect, useRef } from "react";
import { cfdHttpBase, fetchDeskJson, sbHttpBase } from "@/lib/desk-api-bases";

type RestBudgetPayload = {
  ok?: boolean;
  token_buckets?: Record<
    string,
    {
      tokens_available?: number;
      capacity?: number;
      refill_rate?: number;
    }
  >;
  rest_api_budget?: {
    calls_last_minute?: number;
    pressure_level?: string;
  };
  pressure_level?: string;
};

type LaneSpec = {
  id: "cfd" | "sb";
  label: string;
  accountId: string;
  rateCap: number;
  capacity: number;
};

const LANES: LaneSpec[] = [
  { id: "cfd", label: "CFD REST", accountId: "Z6BAH4", rateCap: 40, capacity: 40 },
  { id: "sb", label: "SB REST", accountId: "Z6BAH3", rateCap: 10, capacity: 10 },
];

const POLL_MS = 5000;

type BarRefs = {
  fill: HTMLDivElement | null;
  rate: HTMLSpanElement | null;
  tokens: HTMLSpanElement | null;
};

function bucketPct(
  payload: RestBudgetPayload | null,
  lane: LaneSpec,
  online: boolean,
): { pct: number; tokens: number; capacity: number; rateLabel: string } {
  const cap = lane.capacity;
  const buckets = payload?.token_buckets ?? {};
  const igRest = buckets.ig_rest;
  let tokens = igRest?.tokens_available;
  let capacity = igRest?.capacity ?? cap;

  if (tokens == null || !Number.isFinite(tokens)) {
    const calls = Number(payload?.rest_api_budget?.calls_last_minute ?? 0);
    const drain = Math.min(cap, calls * 0.15);
    tokens = Math.max(0, cap - drain);
    capacity = cap;
  }

  if (!online) {
    tokens = 0;
  }

  const pct = capacity > 0 ? Math.min(100, Math.max(0, (tokens / capacity) * 100)) : 0;
  const rateLabel = `${lane.rateCap}/s · cap ${lane.capacity}`;

  return { pct, tokens, capacity, rateLabel };
}

function paintBar(refs: BarRefs, lane: LaneSpec, snap: ReturnType<typeof bucketPct>): void {
  if (refs.fill) {
    refs.fill.style.width = `${snap.pct.toFixed(1)}%`;
    refs.fill.dataset.tone =
      snap.pct > 55 ? "live" : snap.pct > 20 ? "warn" : "bad";
  }
  if (refs.rate) {
    refs.rate.textContent = snap.rateLabel;
  }
  if (refs.tokens) {
    refs.tokens.textContent = `${snap.tokens.toFixed(1)} / ${snap.capacity.toFixed(0)} tok`;
  }
}

export function TokenBucketProgressBars() {
  const cfdRefs = useRef<BarRefs>({ fill: null, rate: null, tokens: null });
  const sbRefs = useRef<BarRefs>({ fill: null, rate: null, tokens: null });
  const pressureRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    let alive = true;
    const cfdBase = cfdHttpBase();
    const sbBase = sbHttpBase();

    const pull = async () => {
      const [cfdRes, sbRes] = await Promise.all([
        fetchDeskJson<RestBudgetPayload>(
          cfdBase,
          "/api/desk/rest_budget",
          undefined,
          2500,
        )
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
        fetchDeskJson<RestBudgetPayload>(
          sbBase,
          "/api/desk/rest_budget",
          undefined,
          2500,
        )
          .then((p) => ({ ok: true as const, payload: p }))
          .catch(() => ({ ok: false as const, payload: null })),
      ]);
      if (!alive) return;

      paintBar(cfdRefs.current, LANES[0]!, bucketPct(cfdRes.payload, LANES[0]!, cfdRes.ok));
      paintBar(sbRefs.current, LANES[1]!, bucketPct(sbRes.payload, LANES[1]!, sbRes.ok));

      const levels = [
        cfdRes.payload?.pressure_level ?? cfdRes.payload?.rest_api_budget?.pressure_level,
        sbRes.payload?.pressure_level ?? sbRes.payload?.rest_api_budget?.pressure_level,
      ]
        .filter(Boolean)
        .map((v) => String(v).toUpperCase());
      const worst =
        levels.includes("CRITICAL")
          ? "CRITICAL"
          : levels.includes("HIGH")
            ? "HIGH"
            : levels.includes("ELEVATED")
              ? "ELEVATED"
              : levels[0] || "IDLE";
      if (pressureRef.current) {
        pressureRef.current.textContent = `REST ${worst}`;
        pressureRef.current.dataset.tone =
          worst === "IDLE" ? "live" : worst === "ELEVATED" ? "warn" : "bad";
      }
    };

    void pull();
    const id = window.setInterval(pull, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  return (
    <section className="v36-token-panel" aria-label="Account token bucket meters">
      <header className="v36-strip-head">
        <p className="gpu-kicker">Token Buckets</p>
        <h3 className="v36-strip-title">REST Drain / Refill</h3>
        <span
          ref={pressureRef}
          className="gpu-chip gpu-chip--mono v36-token-pressure"
          data-tone="live"
        >
          REST IDLE
        </span>
      </header>
      {LANES.map((lane, idx) => {
        const refs = idx === 0 ? cfdRefs : sbRefs;
        return (
          <div key={lane.id} className="v36-token-lane">
            <div className="v36-token-lane-head">
              <span className="v36-token-label">
                {lane.label} · {lane.accountId}
              </span>
              <span
                ref={(el) => {
                  refs.current.rate = el;
                }}
                className="v36-token-rate"
              >
                {lane.rateCap}/s · cap {lane.capacity}
              </span>
            </div>
            <div className="v36-token-track" aria-hidden>
              <div
                ref={(el) => {
                  refs.current.fill = el;
                }}
                className="v36-token-fill"
                data-tone="live"
              />
            </div>
            <span
              ref={(el) => {
                refs.current.tokens = el;
              }}
              className="v36-token-meta"
            >
              — / {lane.capacity} tok
            </span>
          </div>
        );
      })}
    </section>
  );
}
