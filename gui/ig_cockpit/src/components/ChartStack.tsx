import { useEffect, useRef } from "react";
import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useCockpit } from "../hooks/CockpitProvider";
import { regimeColor } from "../lib/regimeTheme";

export default function ChartStack() {
  const { chart, loading } = useCockpit();
  const priceRef = useRef<HTMLDivElement>(null);
  const pnlRef = useRef<HTMLDivElement>(null);
  const priceChartRef = useRef<IChartApi | null>(null);
  const pnlChartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const pnlSeriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const upperBandRef = useRef<ISeriesApi<"Line"> | null>(null);
  const lowerBandRef = useRef<ISeriesApi<"Line"> | null>(null);
  const syncing = useRef(false);
  const pendingRef = useRef<{ candles: typeof chart.candleHistory; pnl: typeof chart.pnlHistory; markers: typeof chart.markers } | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastCandleLen = useRef(0);
  const lastPnlLen = useRef(0);

  useEffect(() => {
    if (!priceRef.current || !pnlRef.current) return;

    const priceChart = createChart(priceRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#94a3b8",
      },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      width: priceRef.current.clientWidth,
      height: priceRef.current.clientHeight,
      timeScale: { borderColor: "#2a3544" },
      rightPriceScale: { borderColor: "#2a3544" },
      crosshair: { mode: 1 },
    });

    const pnlChart = createChart(pnlRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#94a3b8",
      },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      width: pnlRef.current.clientWidth,
      height: pnlRef.current.clientHeight,
      timeScale: { borderColor: "#2a3544" },
      rightPriceScale: { borderColor: "#2a3544" },
      crosshair: { mode: 1 },
    });

    const candleSeries = priceChart.addCandlestickSeries({
      upColor: "#34d399",
      downColor: "#f87171",
      borderVisible: false,
      wickUpColor: "#34d399",
      wickDownColor: "#f87171",
    });

    const pnlSeries = pnlChart.addAreaSeries({
      lineColor: "#38bdf8",
      topColor: "rgba(56, 189, 248, 0.35)",
      bottomColor: "rgba(56, 189, 248, 0.02)",
      lineWidth: 2,
    });

    const upperBand = pnlChart.addLineSeries({
      color: "rgba(52, 211, 153, 0.5)",
      lineWidth: 1,
      lineStyle: 2,
    });
    const lowerBand = pnlChart.addLineSeries({
      color: "rgba(248, 113, 113, 0.5)",
      lineWidth: 1,
      lineStyle: 2,
    });

    priceChartRef.current = priceChart;
    pnlChartRef.current = pnlChart;
    candleSeriesRef.current = candleSeries;
    pnlSeriesRef.current = pnlSeries;
    upperBandRef.current = upperBand;
    lowerBandRef.current = lowerBand;

    const syncCrosshair = (
      source: IChartApi,
      target: IChartApi,
      targetSeries: ISeriesApi<"Area"> | ISeriesApi<"Candlestick">,
    ) => {
      source.subscribeCrosshairMove((param) => {
        if (syncing.current) return;
        if (!param.time) {
          target.clearCrosshairPosition();
          return;
        }
        syncing.current = true;
        const entries = param.seriesData.values();
        const first = entries.next().value as { close?: number; value?: number } | undefined;
        const val = first?.close ?? first?.value;
        if (val !== undefined) {
          target.setCrosshairPosition(val, param.time, targetSeries);
        }
        syncing.current = false;
      });
    };

    syncCrosshair(priceChart, pnlChart, pnlSeries);
    syncCrosshair(pnlChart, priceChart, candleSeries);

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.target === priceRef.current) {
          priceChart.applyOptions({
            width: entry.contentRect.width,
            height: entry.contentRect.height,
          });
        }
        if (entry.target === pnlRef.current) {
          pnlChart.applyOptions({
            width: entry.contentRect.width,
            height: entry.contentRect.height,
          });
        }
      }
    });
    ro.observe(priceRef.current);
    ro.observe(pnlRef.current);

    return () => {
      ro.disconnect();
      priceChart.remove();
      pnlChart.remove();
      priceChartRef.current = null;
      pnlChartRef.current = null;
    };
  }, []);

  useEffect(() => {
    pendingRef.current = {
      candles: chart.candleHistory,
      pnl: chart.pnlHistory,
      markers: chart.markers,
    };
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const pending = pendingRef.current;
      if (!pending) return;

      const candleSeries = candleSeriesRef.current;
      const pnlSeries = pnlSeriesRef.current;
      if (!candleSeries || !pnlSeries) return;

      if (pending.candles.length) {
        const last = pending.candles[pending.candles.length - 1];
        const point = {
          time: last.time as UTCTimestamp,
          open: last.open,
          high: last.high,
          low: last.low,
          close: last.close,
        };
        if (
          pending.candles.length === lastCandleLen.current &&
          lastCandleLen.current > 0
        ) {
          candleSeries.update(point);
        } else {
          candleSeries.setData(
            pending.candles.map((c) => ({
              time: c.time as UTCTimestamp,
              open: c.open,
              high: c.high,
              low: c.low,
              close: c.close,
            })),
          );
          lastCandleLen.current = pending.candles.length;
        }
        const lwMarkers: SeriesMarker<Time>[] = pending.markers.map((m) => ({
          time: m.time as UTCTimestamp,
          position: m.position,
          color: m.color,
          shape: m.shape,
          text: m.text,
        }));
        candleSeries.setMarkers(lwMarkers);
      }

      if (pending.pnl.length) {
        const lastP = pending.pnl[pending.pnl.length - 1];
        const pPoint = { time: lastP.time as UTCTimestamp, value: lastP.value };
        if (pending.pnl.length === lastPnlLen.current && lastPnlLen.current > 0) {
          pnlSeries.update(pPoint);
        } else {
          pnlSeries.setData(
            pending.pnl.map((p) => ({
              time: p.time as UTCTimestamp,
              value: p.value,
            })),
          );
          lastPnlLen.current = pending.pnl.length;
        }

        if (chart.riskBands.length > 0 && upperBandRef.current && lowerBandRef.current) {
          const band = chart.riskBands[0];
          const times = pending.pnl.map((p) => p.time as UTCTimestamp);
          if (times.length >= 2) {
            upperBandRef.current.setData(times.map((t) => ({ time: t, value: band.upper })));
            lowerBandRef.current.setData(times.map((t) => ({ time: t, value: band.lower })));
          }
        }
      }

      priceChartRef.current?.timeScale().scrollToRealTime();
      pnlChartRef.current?.timeScale().scrollToRealTime();
    });
  }, [chart.candleHistory, chart.pnlHistory, chart.markers, chart.riskBands]);

  const regimeBg = regimeColor(chart.regime);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div
        className="panel relative min-h-0 flex-[2] overflow-hidden transition-colors duration-700 gpu-layer"
        style={{ backgroundColor: regimeBg }}
      >
        <div className="panel-header relative z-10 bg-surface/80 backdrop-blur-sm">
          <span>Price</span>
          <span className="font-mono text-[10px] normal-case text-muted">
            {chart.epic} · {chart.regime}
            {loading ? " · sync" : chart.candleHistory.length ? "" : " · awaiting ticks"}
          </span>
        </div>
        <div ref={priceRef} className="relative z-10 min-h-0 flex-1 transform-gpu" />
      </div>

      <div className="panel min-h-0 flex-1 overflow-hidden gpu-layer">
        <div className="panel-header">
          <span>Session P&amp;L &amp; Risk Envelope</span>
          <span className="font-mono text-[10px] normal-case text-muted">
            {chart.currentPnl !== null ? `£${chart.currentPnl.toFixed(2)}` : "GBP"}
            {chart.targetPnl !== null ? ` / target ${chart.targetPnl}` : ""}
          </span>
        </div>
        <div ref={pnlRef} className="min-h-0 flex-1 transform-gpu" />
      </div>
    </div>
  );
}
