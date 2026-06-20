const EMPTY_MSG = "Awaiting initial market telemetry streams…";

/**
 * Donut allocation pie — asset confidence / activity weights.
 * @param {{ segments?: Array<{ label: string, value: number, color: string }> | null }} props
 */
export default function AllocationPieChart({ segments }) {
  const safeSegments = sanitizeSegments(segments);
  const total = safeSegments.reduce((s, seg) => s + Math.max(0, Number(seg?.value) || 0), 0);

  const radius = 42;
  const cx = 52;
  const cy = 52;
  const stroke = 14;

  // Safe Frontend Fallback: empty boot handshake — neon indigo placeholder ring
  if (!safeSegments.length || total <= 0) {
    return (
      <div className="apex-pie apex-pie--empty apex-chart-empty">
        <div className="apex-pie__label">Asset Activity Mix</div>
        <div className="apex-pie__body apex-pie__body--empty">
          <svg
            viewBox="0 0 104 104"
            className="apex-pie__svg apex-pie__svg--empty"
            role="img"
            aria-label="Awaiting asset allocation data"
          >
            <circle
              cx={cx}
              cy={cy}
              r={radius}
              fill="none"
              stroke="#1c234a"
              strokeWidth={stroke}
              opacity={0.85}
            />
            <circle
              cx={cx}
              cy={cy}
              r={radius - stroke / 2 - 2}
              fill="#0d1330"
              stroke="rgba(0, 180, 216, 0.25)"
              strokeWidth={1}
            />
          </svg>
          <span className="apex-pie__placeholder">{EMPTY_MSG}</span>
        </div>
      </div>
    );
  }

  let angle = -Math.PI / 2;
  const arcs = safeSegments
    .filter((s) => (Number(s?.value) || 0) > 0)
    .map((seg) => {
      const val = Math.max(0, Number(seg?.value) || 0);
      const slice = (val / total) * Math.PI * 2;
      const start = angle;
      angle += slice;
      const end = angle;
      const x1 = cx + radius * Math.cos(start);
      const y1 = cy + radius * Math.sin(start);
      const x2 = cx + radius * Math.cos(end);
      const y2 = cy + radius * Math.sin(end);
      const large = slice > Math.PI ? 1 : 0;
      const d = `M ${x1} ${y1} A ${radius} ${radius} 0 ${large} 1 ${x2} ${y2}`;
      const label = seg?.label ?? "—";
      return {
        label,
        color: seg?.color ?? "#6b7c96",
        d,
        pct: ((val / total) * 100).toFixed(1),
      };
    });

  if (!arcs.length) {
    return (
      <div className="apex-pie apex-pie--empty apex-chart-empty">
        <div className="apex-pie__label">Asset Activity Mix</div>
        <span className="apex-pie__placeholder">{EMPTY_MSG}</span>
      </div>
    );
  }

  return (
    <div className="apex-pie">
      <div className="apex-pie__label">Asset Activity Mix</div>
      <div className="apex-pie__body">
        <svg viewBox="0 0 104 104" className="apex-pie__svg" role="img" aria-label="Asset activity pie chart">
          {arcs.map((arc) => (
            <path
              key={arc.label}
              d={arc.d}
              fill="none"
              stroke={arc.color}
              strokeWidth={stroke}
              strokeLinecap="butt"
            />
          ))}
          <circle cx={cx} cy={cy} r={radius - stroke / 2 - 2} fill="#0d1330" />
          <text x={cx} y={cy - 2} textAnchor="middle" className="apex-pie__center-val">
            {Math.round(total)}
          </text>
          <text x={cx} y={cy + 10} textAnchor="middle" className="apex-pie__center-sub">
            pts
          </text>
        </svg>
        <ul className="apex-pie__legend">
          {arcs.map((arc) => (
            <li key={arc.label}>
              <span className="apex-pie__dot" style={{ background: arc?.color ?? "#6b7c96" }} />
              <span className="apex-pie__legend-label">{arc.label}</span>
              <span className="apex-pie__legend-val">{arc.pct}%</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/**
 * @param {unknown} raw
 * @returns {Array<{ label: string, value: number, color: string }>}
 */
function sanitizeSegments(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((seg) => seg != null && typeof seg === "object")
    .map((seg) => ({
      label: String(seg.label ?? "—"),
      value: Math.max(0, Number(seg.value) || 0),
      color: String(seg.color ?? "#6b7c96"),
    }));
}
