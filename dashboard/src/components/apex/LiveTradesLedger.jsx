import { useEffect, useState } from "react";
import { subscribeLedger } from "../../api/apexIpc.js";
import { fmtPrice } from "../../utils/fmtPrice.js";

/**
 * AVIONICS LIVE TRADES LEDGER — Worker D execution packets via apex_ipc.sock.
 */
export default function LiveTradesLedger() {
  const [rows, setRows] = useState([]);

  useEffect(() => {
    return subscribeLedger((packet) => {
      if (!packet || typeof packet !== "object") return;
      setRows((prev) => {
        const next = [
          {
            id: `${packet.deal_id || packet.deal_reference || packet.ts}-${Date.now()}`,
            ts: packet.ts_iso || packet.ts || new Date().toISOString(),
            epic: String(packet.epic || "—"),
            action: String(packet.action || packet.side || "—").toUpperCase(),
            size: packet.size != null ? Math.trunc(Number(packet.size)) : "—",
            entry: packet.entry ?? packet.entry_price ?? packet.level,
            latencyMs: packet.latency_ms ?? packet.latencyMs,
            mode: packet.mode || packet.source || "—",
          },
          ...prev,
        ];
        return next.slice(0, 50);
      });
    });
  }, []);

  return (
    <section className="apex-ledger" aria-label="AVIONICS LIVE TRADES LEDGER">
      <header className="apex-ledger__header">
        <h2>AVIONICS LIVE TRADES LEDGER</h2>
        <span className="apex-ledger__sub">Worker D · apex_ipc.sock</span>
      </header>
      <div className="apex-ledger__table-wrap">
        <table className="apex-ledger__table">
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Epic</th>
              <th>Action</th>
              <th>Size</th>
              <th>Entry</th>
              <th>Latency</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={6} className="apex-ledger__empty">
                  Awaiting broker confirmation packets…
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr key={row.id} className="apex-ledger__row--new">
                  <td>{formatTs(row.ts)}</td>
                  <td className="apex-ledger__epic">{row.epic}</td>
                  <td className={row.action.includes("BUY") ? "apex-ledger__buy" : row.action.includes("SELL") ? "apex-ledger__sell" : ""}>
                    {row.action}
                  </td>
                  <td>{row.size}</td>
                  <td>{row.entry != null ? fmtPrice(row.entry, row.epic) : "—"}</td>
                  <td>{row.latencyMs != null ? `${Number(row.latencyMs).toFixed(1)} ms` : "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function formatTs(ts) {
  try {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return String(ts);
    return d.toLocaleTimeString("en-GB", { hour12: false, fractionalSecondDigits: 3 });
  } catch {
    return String(ts);
  }
}
