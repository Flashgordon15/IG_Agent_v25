"use client";

import { useRef, type MutableRefObject } from "react";
import { QuantumRouterAuditModal } from "@/components/QuantumRouterAuditModal";
import type { DeskCapitalView } from "@/hooks/useDeskCapital";
import type {
  ScannerRankedChrome,
  SniperMarketRow,
  SniperRankLane,
} from "@/lib/quantum-node-types";

type Props = {
  rows: SniperMarketRow[];
  wsState: string;
  capital: DeskCapitalView;
  muxOpenCount: number;
  /** One desk-level idle reason — avoids four identical yellow idle boxes */
  deskIdleReason?: string | null;
  rankedChrome?: ScannerRankedChrome | null;
};

function rankLaneLabel(lane: SniperRankLane): string | null {
  if (lane === "promoted") return "PROMOTED";
  if (lane === "eligible") return "ELIGIBLE";
  if (lane === "waiting") return "WAITING";
  if (lane === "excluded") return "EXCLUDED";
  if (lane === "stack") return "STACK";
  return null;
}

/** Desk sniper arm threshold — below = chop isolation (slate); at/above = emerald flash */
const SNIPER_GATE = 0.68;

function fmtMid(row: SniperMarketRow): string {
  if (!(row.mid > 0)) return "—";
  if (row.id === "eurusd") return row.mid.toFixed(5);
  if (row.id === "gold" || row.id === "brent") return row.mid.toFixed(2);
  return row.mid.toFixed(1);
}

function statusClass(kind: SniperMarketRow["statusKind"]): string {
  if (kind === "long") return "sniper-flag sniper-flag--long";
  if (kind === "short") return "sniper-flag sniper-flag--short";
  return "sniper-flag sniper-flag--proxy";
}

function RegimeScoreCell({
  conviction,
  statusKind,
  sniperThreshold = SNIPER_GATE,
  rollingRef,
}: {
  id: string;
  conviction: number | null;
  statusKind: SniperMarketRow["statusKind"];
  sniperThreshold?: number;
  rollingRef: MutableRefObject<number[]>;
}) {
  if (conviction != null && Number.isFinite(conviction)) {
    const hist = rollingRef.current;
    hist.push(Math.max(0, Math.min(1, conviction)));
    if (hist.length > 10) hist.shift();
  }
  const smoothed =
    rollingRef.current.length > 0
      ? rollingRef.current.reduce((a, b) => a + b, 0) / rollingRef.current.length
      : conviction;
  const p =
    smoothed != null && Number.isFinite(smoothed)
      ? Math.max(0, Math.min(1, smoothed))
      : null;
  const pct = p != null ? Math.round(p * 100) : null;
  const gate =
    sniperThreshold != null && Number.isFinite(sniperThreshold)
      ? sniperThreshold
      : SNIPER_GATE;
  const mlEdge = p != null && p >= gate;
  // Strategy armed only when status is long/short engaged — not ML alone
  const strategyArmed = statusKind === "long" || statusKind === "short";
  const armedVisual = strategyArmed || mlEdge;

  return (
    <div
      className={`sniper-conviction ${armedVisual ? "sniper-conviction--armed" : "sniper-conviction--slate"}`}
    >
      <strong className="gpu-ledger-mono sniper-regime-score">
        {pct != null
          ? `AI Matrix Regime Score ${pct}%`
          : "AI Matrix Regime Score —"}
      </strong>
      <span className="sniper-regime-status">
        {strategyArmed
          ? "SNIPER ARMED"
          : mlEdge
            ? "ML EDGE · GATED"
            : "CHOP ISOLATION"}
      </span>
      <div className="gpu-milestone-track" aria-hidden>
        <div
          className="gpu-milestone-fill"
          style={{
            width: `${Math.min(100, Math.max(0, pct ?? 0))}%`,
          }}
        />
      </div>
    </div>
  );
}

function truncateStatus(text: string, max = 72): string {
  const s = String(text || "").trim();
  if (s.length <= max) return s;
  return `${s.slice(0, max - 1)}…`;
}

function OpenTrailCell({ row }: { row: SniperMarketRow }) {
  if (!(row.openCount > 0)) {
    return (
      <div className="sniper-open-trail sniper-open-trail--flat">
        <strong className="gpu-ledger-mono">FLAT</strong>
        <span className="sniper-meta">
          CAP {row.maxSpreadPts}
          {row.isForex ? " PIP" : " PT"}
        </span>
      </div>
    );
  }
  const pnl = row.pnlGbp;
  const pnlTxt =
    pnl == null
      ? "UPL —"
      : `UPL ${pnl >= 0 ? "+" : "−"}£${Math.abs(pnl).toFixed(2)}`;
  const trailTxt =
    row.trailFloorGbp != null && row.trailFloorGbp > 0
      ? `TRAIL £${row.trailFloorGbp.toFixed(2)}`
      : "TRAIL —";
  const levelTxt =
    row.trailPriceLevel != null && row.trailPriceLevel > 0
      ? `@ ${row.isForex ? row.trailPriceLevel.toFixed(5) : row.trailPriceLevel.toFixed(1)}`
      : "";

  return (
    <div className="sniper-open-trail sniper-open-trail--live">
      <strong className="gpu-ledger-mono">
        {row.openDirection || "OPEN"} ×{row.openCount}
      </strong>
      <span className="sniper-meta">{pnlTxt}</span>
      <span className="gpu-ledger-mono sniper-trail-line">
        {trailTxt} {levelTxt}
      </span>
    </div>
  );
}

export function AIMarketScanner({
  rows,
  wsState,
  capital,
  muxOpenCount,
  deskIdleReason = null,
  rankedChrome = null,
}: Props) {
  const regimeHistRef = useRef<Map<string, number[]>>(new Map());
  const progress = Math.min(100, Math.max(0, capital.progressPct));
  const cash = capital.provisionalCashGbp || capital.realizedTodayGbp;
  const allProxyIdle =
    rows.length > 0 &&
    rows.every((r) => r.statusKind === "proxy" && r.openCount === 0);
  const banner =
    deskIdleReason && allProxyIdle
      ? deskIdleReason
      : null;
  const rankedOn = rankedChrome?.active === true;
  const promotedLabels = rankedChrome?.promotedLabels ?? [];
  const waitingLabels = rankedChrome?.waitingLabels ?? [];
  const universeChip = rankedOn
    ? [
        promotedLabels.length
          ? `PROMOTED ${promotedLabels.join(" · ")}`
          : null,
        waitingLabels.length
          ? `WAIT ${waitingLabels.join(" · ")}`
          : null,
        rankedChrome?.excludedNote ?? null,
      ]
        .filter(Boolean)
        .join(" · ")
    : "DOW · FTSE · GOLD · EURUSD";

  return (
    <section className="gpu-fleet-panel sniper-hub" aria-label="AI multi-market scanner">
      <header className="gpu-panel-head">
        <div>
          <p className="gpu-kicker">Multi-Market AI Strategy Hub</p>
          <h2 className="gpu-panel-title">AI Market Scanner</h2>
        </div>
        <div className="gpu-tensor-chips">
          <span className="gpu-chip gpu-chip--live">WS {wsState.toUpperCase()}</span>
          {rankedOn ? (
            <span className="gpu-chip gpu-chip--ok" title="Ranked multi-market rotator">
              Ranked rotator ON
              {rankedChrome?.dominant ? ` · ${rankedChrome.dominant}` : ""}
            </span>
          ) : null}
          <span className="gpu-chip" title={universeChip}>
            {universeChip}
          </span>
          <span className="gpu-chip">MUX OPEN {muxOpenCount}</span>
          <span className="gpu-chip">GATE ADAPTIVE</span>
          <QuantumRouterAuditModal triggerClassName="blueprint-trigger blueprint-trigger--router" />
        </div>
      </header>

      {banner ? (
        <div className="sniper-desk-idle" role="status">
          <span className="gpu-kicker">DESK IDLE</span>
          <strong className="sniper-desk-idle-label">{banner}</strong>
          <span className="sniper-meta">
            Agent trade-ready — entries waiting on gate/session, not a crash
          </span>
        </div>
      ) : null}

      <div className="sniper-milestone-strip" aria-label="Daily £1000 milestone">
        <div className="sniper-milestone-meta">
          <span className="gpu-ledger-mono">
            £{cash.toFixed(2)} / £{capital.milestoneGbp.toFixed(0)}
          </span>
          <span className="sniper-meta">{progress.toFixed(1)}% TO MILESTONE</span>
        </div>
        <div className="gpu-milestone-track sniper-daily-track" aria-hidden>
          <div
            className="gpu-milestone-fill"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      <div className="sniper-matrix sniper-matrix--memory" role="table">
        <div className="sniper-matrix-head" role="row">
          <span>ASSET</span>
          <span>MID</span>
          <span>OPEN · TRAIL</span>
          <span>AI MATRIX REGIME SCORE</span>
          <span>STRATEGY STATUS</span>
        </div>
        {rows.map((row) => {
          if (!regimeHistRef.current.has(row.id)) {
            regimeHistRef.current.set(row.id, []);
          }
          const rollingRef = {
            current: regimeHistRef.current.get(row.id)!,
          } as MutableRefObject<number[]>;
          const lane = rankLaneLabel(row.rankLane);
          return (
          <div
            key={row.id}
            className="sniper-row"
            role="row"
            data-rank-lane={row.rankLane || undefined}
          >
            <div className="sniper-asset">
              <strong>
                {row.label}
                {lane ? (
                  <span
                    className="sniper-rank-lane"
                    data-lane={row.rankLane || undefined}
                  >
                    {row.rank != null ? `#${row.rank} ` : ""}
                    {lane}
                  </span>
                ) : null}
              </strong>
              <span className="sniper-epic">{row.epic}</span>
              <span className="sniper-meta">
                {row.tpm.toFixed(0)} tpm · z {row.zScore.toFixed(2)} · {row.regimeLabel}
              </span>
            </div>
            <div className="sniper-mid">
              <strong className="gpu-ledger-mono">{fmtMid(row)}</strong>
              <span className="sniper-meta">{row.profile}</span>
            </div>
            <OpenTrailCell row={row} />
            <RegimeScoreCell
              id={row.id}
              conviction={row.conviction}
              statusKind={row.statusKind}
              sniperThreshold={row.sniperThreshold}
              rollingRef={rollingRef}
            />
            <div className={statusClass(row.statusKind)} title={row.statusText}>
              {truncateStatus(row.statusText)}
            </div>
          </div>
          );
        })}
      </div>
    </section>
  );
}
