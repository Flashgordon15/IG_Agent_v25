import { useEffect, useRef } from "react";
import { ApexWebGLRenderer } from "../../webgl/ApexWebGLRenderer.js";

/**
 * Primary HUD WebGL viewport — GPU-offloaded charting layer.
 * @param {{ telemetry: import('../../apex/types.js').ParsedApexTelemetry | null, className?: string }} props
 */
export default function ApexWebGLCanvas({ telemetry, className = "" }) {
  const canvasRef = useRef(null);
  const rendererRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const renderer = new ApexWebGLRenderer(canvas);
    rendererRef.current = renderer;

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        renderer.resize(width, height);
      }
    });
    ro.observe(canvas.parentElement ?? canvas);
    renderer.resize(canvas.clientWidth, canvas.clientHeight);
    renderer.start();

    return () => {
      ro.disconnect();
      renderer.dispose();
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!telemetry?.assets || !rendererRef.current) return;
    rendererRef.current.ingestTelemetry(telemetry.assets);
  }, [telemetry]);

  return (
    <canvas
      ref={canvasRef}
      className={className}
      aria-label="Apex WebGL avionics viewport"
    />
  );
}
