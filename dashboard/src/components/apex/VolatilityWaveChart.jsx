import { useMemo } from "react";

const EMPTY_MSG = "Awaiting initial market telemetry streams…";

/**
 * Smoothed SVG spline volatility wave with teal fill area.
 * @param {{ series: number[] | null | undefined, height?: number, className?: string }} props
 */
export default function VolatilityWaveChart({ series, height = 120, className = "" }) {
  const safeSeries = useMemo(() => sanitizeSeries(series), [series]);

  const paths = useMemo(() => {
    if (!safeSeries || safeSeries.length < 2) return null;
    return buildSplinePaths(safeSeries, height);
  }, [safeSeries, height]);

  // Safe Frontend Fallback: Block parsing loops when data queues are empty on boot
  if (!safeSeries || safeSeries.length < 2 || !paths?.linePath) {
    return (
      <div
        className={`apex-vol-wave apex-vol-wave--empty apex-chart-empty ${className}`}
        style={{ minHeight: height }}
      >
        <div className="apex-vol-wave__label">Historical Volatility Wave</div>
        <span className="apex-vol-wave__placeholder">{EMPTY_MSG}</span>
      </div>
    );
  }

  const { linePath, areaPath } = paths;

  return (
    <div className={`apex-vol-wave ${className}`}>
      <div className="apex-vol-wave__label">Historical Volatility Wave</div>
      <svg
        viewBox={`0 0 320 ${height}`}
        preserveAspectRatio="none"
        className="apex-vol-wave__svg"
        role="img"
        aria-label="Historical volatility wave chart"
      >
        <defs>
          <linearGradient id="apex-vol-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#2ec4b6" stopOpacity="0.38" />
            <stop offset="100%" stopColor="#2ec4b6" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {areaPath ? <path d={areaPath} fill="url(#apex-vol-fill)" stroke="none" /> : null}
        {linePath ? (
          <path
            d={linePath}
            fill="none"
            stroke="#2ec4b6"
            strokeWidth="2.25"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ) : null}
      </svg>
    </div>
  );
}

/**
 * @param {unknown} raw
 * @returns {number[]}
 */
function sanitizeSeries(raw) {
  if (!Array.isArray(raw) || raw.length === 0) return [];
  return raw
    .map((v) => (typeof v === "number" && Number.isFinite(v) ? v : Number(v)))
    .filter((v) => Number.isFinite(v));
}

/**
 * @param {number[]} values
 * @param {number} height
 * @returns {{ linePath: string, areaPath: string } | null}
 */
function buildSplinePaths(values, height) {
  if (!values?.length || values.length < 2) return null;

  const w = 320;
  const pad = 8;
  const innerH = Math.max(1, height - pad * 2);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const denom = Math.max(1, values.length - 1);

  const pts = values.map((v, i) => ({
    x: pad + (i / denom) * (w - pad * 2),
    y: pad + innerH - (((Number(v) || 0) - min) / span) * innerH,
  }));

  if (pts.length < 2 || !Number.isFinite(pts[0]?.x)) return null;

  const linePath = catmullRomPath(pts);
  if (!linePath) return null;

  const last = pts[pts.length - 1];
  const first = pts[0];
  const areaPath = `${linePath} L ${last?.x ?? pad} ${height - pad} L ${first?.x ?? pad} ${height - pad} Z`;
  return { linePath, areaPath };
}

/**
 * @param {Array<{ x?: number, y?: number }>} pts
 * @returns {string}
 */
function catmullRomPath(pts) {
  if (!Array.isArray(pts) || pts.length < 2) return "";

  const x0 = pts[0]?.x ?? 0;
  const y0 = pts[0]?.y ?? 0;
  if (!Number.isFinite(x0) || !Number.isFinite(y0)) return "";

  let d = `M ${x0} ${y0}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)] ?? pts[0];
    const p1 = pts[i] ?? pts[0];
    const p2 = pts[i + 1] ?? pts[pts.length - 1];
    const p3 = pts[Math.min(pts.length - 1, i + 2)] ?? p2;

    const cp1x = (p1?.x ?? 0) + ((p2?.x ?? 0) - (p0?.x ?? 0)) / 6;
    const cp1y = (p1?.y ?? 0) + ((p2?.y ?? 0) - (p0?.y ?? 0)) / 6;
    const cp2x = (p2?.x ?? 0) - ((p3?.x ?? 0) - (p1?.x ?? 0)) / 6;
    const cp2y = (p2?.y ?? 0) - ((p3?.y ?? 0) - (p1?.y ?? 0)) / 6;
    d += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${p2?.x ?? 0} ${p2?.y ?? 0}`;
  }
  return d;
}
