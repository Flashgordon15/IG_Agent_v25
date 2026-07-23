"use client";

/**
 * v32 dual-lane control deck — per-engine header cards + ref-driven blotters.
 * Lanes bound from dual-port buffer (:8080 CFD · :8081 SB).
 */

import { useEffect, useRef, useState, type MutableRefObject } from "react";
import type { GpuExecutionBuffer, GpuExecPosition } from "@/lib/gpu-execution-buffer";
import { laneOperational, type DeskEngineLane } from "@/lib/desk-multiplex";

const OFFLINE_PLACEHOLDER_LANES: DeskEngineLane[] = [
  {
    engineId: "cfd_sniper",
    label: "QUANT SNIPER (CFD - Z6BAH4)",
    accountId: "Z6BAH4",
    productType: "CFD",
    engineOrigin: "QUANT_SNIPER",
    quoteAgeMs: null,
    quoteBudgetMs: 10_000,
    transport: "rest_poll",
    pathLive: false,
    standby: false,
    openCount: 0,
    positions: [],
    operational: false,
  },
  {
    engineId: "sb_sentinel",
    label: "MACRO SENTINEL (SB - Z6BAH3)",
    accountId: "Z6BAH3",
    productType: "SPREADBET",
    engineOrigin: "MACRO_SENTINEL",
    quoteAgeMs: null,
    quoteBudgetMs: 10_000,
    transport: "rest_poll",
    pathLive: false,
    standby: false,
    openCount: 0,
    positions: [],
    operational: false,
  },
];

type CellRefs = {
  deal: HTMLSpanElement | null;
  asset: HTMLSpanElement | null;
  side: HTMLSpanElement | null;
  size: HTMLSpanElement | null;
  upl: HTMLSpanElement | null;
};

type Props = {
  bufferRef: MutableRefObject<GpuExecutionBuffer>;
  structureRevision: number;
  coreDetached?: boolean;
};

function fmtUpl(v: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
}

function paintRow(cells: CellRefs, p: GpuExecPosition): void {
  if (cells.deal) cells.deal.textContent = p.dealId.slice(0, 14);
  if (cells.asset) cells.asset.textContent = p.label || p.epic;
  if (cells.side) {
    cells.side.textContent = p.direction === "SELL" ? "SHORT" : "LONG";
    cells.side.dataset.side = p.direction === "SELL" ? "short" : "long";
  }
  if (cells.size) cells.size.textContent = p.size.toFixed(2);
  if (cells.upl) {
    cells.upl.textContent = fmtUpl(p.pnlGbp);
    cells.upl.dataset.tone =
      p.pnlGbp == null ? "mute" : p.pnlGbp >= 0 ? "profit" : "loss";
  }
}

function LaneBlotter({
  lane,
  coreDetached,
}: {
  lane: DeskEngineLane;
  coreDetached?: boolean;
}) {
  const [dealIds, setDealIds] = useState<string[]>([]);
  const cellsRef = useRef(new Map<string, CellRefs>());
  const positionsRef = useRef<GpuExecPosition[]>(lane.positions);
  positionsRef.current = lane.positions;

  useEffect(() => {
    const ids = lane.positions.map((p) => p.dealId);
    setDealIds(ids);
    for (const id of [...cellsRef.current.keys()]) {
      if (!ids.includes(id)) cellsRef.current.delete(id);
    }
  }, [lane.engineId, lane.openCount, lane.positions]);

  useEffect(() => {
    let alive = true;
    const loop = () => {
      if (!alive) return;
      for (const p of positionsRef.current) {
        const cells = cellsRef.current.get(p.dealId);
        if (cells) paintRow(cells, p);
      }
      requestAnimationFrame(loop);
    };
    const raf = requestAnimationFrame(loop);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
    };
  }, [lane.engineId]);

  const bind =
    (dealId: string, key: keyof CellRefs) => (el: HTMLSpanElement | null) => {
      let cells = cellsRef.current.get(dealId);
      if (!cells) {
        cells = { deal: null, asset: null, side: null, size: null, upl: null };
        cellsRef.current.set(dealId, cells);
      }
      cells[key] = el;
    };

  const portDown = lane.pathLive === false;
  const flat = coreDetached || portDown || lane.openCount === 0 || dealIds.length === 0;
  const emptyCopy = coreDetached
    ? "[🛠️ MAINTENANCE DETACHED — BLOTTER PLACEHOLDER]"
    : portDown
    ? "[🚨 PORT OFFLINE — ENGINE UNREACHABLE]"
    : "[📉 NO OPEN EXPOSURE - CAPITAL FULLY RECONCILED]";

  return (
    <div className="dual-lane-blotter" role="table" aria-label={`${lane.label} blotter`}>
      <div className="ref-blotter-head dual-lane-blotter-head" role="row">
        <span>DEAL</span>
        <span>ASSET</span>
        <span>SIDE</span>
        <span>SIZE</span>
        <span>UPL</span>
      </div>
      <div className="ref-blotter-body dual-lane-blotter-body">
        {flat ? (
          <div className="ref-blotter-empty ref-blotter-empty--sovereign dual-lane-empty">
            {emptyCopy}
          </div>
        ) : (
          dealIds.map((id) => (
            <div key={id} className="ref-blotter-row dual-lane-blotter-row" role="row">
              <span ref={bind(id, "deal")} className="ref-blotter-cell ref-blotter-cell--mono" />
              <span ref={bind(id, "asset")} className="ref-blotter-cell ref-blotter-cell--mono" />
              <span ref={bind(id, "side")} className="ref-blotter-cell ref-blotter-cell--side" />
              <span ref={bind(id, "size")} className="ref-blotter-cell ref-blotter-cell--mono" />
              <span ref={bind(id, "upl")} className="ref-blotter-cell ref-blotter-cell--upl" />
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export function DualLaneControlDeck({
  bufferRef,
  structureRevision,
  coreDetached,
}: Props) {
  const [lanes, setLanes] = useState<DeskEngineLane[]>([]);

  useEffect(() => {
    let alive = true;
    const sync = () => {
      if (!alive) return;
      const buf = bufferRef.current;
      const next = buf.engines?.length >= 2 ? buf.engines : OFFLINE_PLACEHOLDER_LANES;
      setLanes(next);
    };
    sync();
    const id = window.setInterval(sync, 500);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [bufferRef, structureRevision]);

  return (
    <section className="dual-lane-deck" aria-label="Dual-engine control deck">
      <div className="dual-lane-grid">
        {lanes.map((lane) => (
          <article
            key={lane.engineId}
            className={`dual-lane-card ${laneOperational(lane) ? "dual-lane-card--live" : "dual-lane-card--idle"}`}
          >
            <header className="dual-lane-card-head">
              <h3 className="dual-lane-title gpu-ledger-mono">{lane.label}</h3>
              <div className="gpu-tensor-chips">
                <span className="gpu-chip gpu-chip--mono">
                  {lane.pathLive === false
                    ? "PORT OFFLINE"
                    : `${lane.openCount} OPEN`}
                </span>
                <span className="gpu-chip gpu-chip--mono">
                  {lane.quoteAgeMs != null
                    ? `${Math.round(lane.quoteAgeMs)}ms / ${lane.quoteBudgetMs}ms`
                    : "QUOTE —"}
                </span>
              </div>
            </header>
            <LaneBlotter lane={lane} coreDetached={coreDetached} />
          </article>
        ))}
      </div>
    </section>
  );
}
