"use client";

/**
 * Ref-driven virtualized position blotter.
 * Structure changes (deal set) may remount rows; tick fields mutate via element refs.
 */

import { useEffect, useRef, useState, type MutableRefObject } from "react";
import type { GpuExecutionBuffer, GpuExecPosition } from "@/lib/gpu-execution-buffer";

type CellRefs = {
  deal: HTMLSpanElement | null;
  asset: HTMLSpanElement | null;
  side: HTMLSpanElement | null;
  size: HTMLSpanElement | null;
  upl: HTMLSpanElement | null;
  trail: HTMLSpanElement | null;
};

type Props = {
  bufferRef: MutableRefObject<GpuExecutionBuffer>;
  structureRevision: number;
  openCount: number;
};

function fmtUpl(v: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}£${Math.abs(v).toFixed(2)}`;
}

function fmtTrail(p: GpuExecPosition): string {
  if (p.trailFloorGbp != null && p.trailFloorGbp > 0) {
    return `£${p.trailFloorGbp.toFixed(2)}`;
  }
  if (p.softLossGbp != null && Number.isFinite(p.softLossGbp)) {
    return `soft £${Math.abs(p.softLossGbp).toFixed(2)}`;
  }
  return "—";
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
  if (cells.trail) cells.trail.textContent = fmtTrail(p);
}

export function RefPositionBlotter({
  bufferRef,
  structureRevision,
  openCount,
}: Props) {
  const [dealIds, setDealIds] = useState<string[]>([]);
  const cellsRef = useRef(new Map<string, CellRefs>());
  const rafRef = useRef(0);

  // Remount row skeleton only when deal set changes
  useEffect(() => {
    const ids = bufferRef.current.positions.map((p) => p.dealId);
    setDealIds(ids);
    // Drop stale cell maps
    for (const id of [...cellsRef.current.keys()]) {
      if (!ids.includes(id)) cellsRef.current.delete(id);
    }
  }, [bufferRef, structureRevision, openCount]);

  // Hot path: mutate DOM text — no React setState
  useEffect(() => {
    let alive = true;
    const loop = () => {
      if (!alive) return;
      const positions = bufferRef.current.positions;
      for (const p of positions) {
        const cells = cellsRef.current.get(p.dealId);
        if (cells) paintRow(cells, p);
      }
      rafRef.current = requestAnimationFrame(loop);
    };
    rafRef.current = requestAnimationFrame(loop);
    return () => {
      alive = false;
      cancelAnimationFrame(rafRef.current);
    };
  }, [bufferRef]);

  const bind =
    (dealId: string, key: keyof CellRefs) => (el: HTMLSpanElement | null) => {
      let cells = cellsRef.current.get(dealId);
      if (!cells) {
        cells = {
          deal: null,
          asset: null,
          side: null,
          size: null,
          upl: null,
          trail: null,
        };
        cellsRef.current.set(dealId, cells);
      }
      cells[key] = el;
    };

  return (
    <section className="ref-blotter desk-section-open" aria-label="Open positions">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker desk-section-kicker">Open positions</p>
          <h2 className="gpu-panel-title">Live position blotter</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip">{openCount} OPEN</span>
          <span className="gpu-chip gpu-chip--mono">DOM REFS · NO TICK RERENDER</span>
        </div>
      </header>

      <div className="ref-blotter-table" role="table">
        <div className="ref-blotter-head" role="row">
          <span>DEAL ID</span>
          <span>ASSET</span>
          <span>SIDE</span>
          <span>SIZE</span>
          <span>LIVE UPL</span>
          <span>TRAIL STOP</span>
        </div>
        <div className="ref-blotter-body">
          {dealIds.length === 0 || openCount === 0 ? (
            <div className="ref-blotter-empty ref-blotter-empty--sovereign">
              [📉 NO OPEN POSITION EXPOSURE - CAPITAL FULLY RECONCILED]
            </div>
          ) : (
            dealIds.map((id) => (
              <div key={id} className="ref-blotter-row" role="row" data-deal={id}>
                <span
                  ref={bind(id, "deal")}
                  className="ref-blotter-cell ref-blotter-cell--mono"
                />
                <span
                  ref={bind(id, "asset")}
                  className="ref-blotter-cell ref-blotter-cell--mono"
                />
                <span
                  ref={bind(id, "side")}
                  className="ref-blotter-cell ref-blotter-cell--side"
                />
                <span
                  ref={bind(id, "size")}
                  className="ref-blotter-cell ref-blotter-cell--mono"
                />
                <span
                  ref={bind(id, "upl")}
                  className="ref-blotter-cell ref-blotter-cell--upl"
                />
                <span
                  ref={bind(id, "trail")}
                  className="ref-blotter-cell ref-blotter-cell--mono"
                />
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  );
}
