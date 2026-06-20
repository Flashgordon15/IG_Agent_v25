import { useEffect, useRef, useState } from "react";

/**
 * Throttle rapid state updates (e.g. 100x replay) to at most once per `intervalMs`.
 */
export default function useThrottledValue(value, intervalMs = 100) {
  const [throttled, setThrottled] = useState(value);
  const lastEmit = useRef(0);
  const pending = useRef(null);
  const valueRef = useRef(value);

  valueRef.current = value;

  useEffect(() => {
    const now = Date.now();
    const elapsed = now - lastEmit.current;

    const flush = () => {
      lastEmit.current = Date.now();
      setThrottled(valueRef.current);
      pending.current = null;
    };

    if (elapsed >= intervalMs) {
      flush();
      return undefined;
    }

    if (pending.current != null) {
      window.clearTimeout(pending.current);
    }
    pending.current = window.setTimeout(flush, intervalMs - elapsed);
    return () => {
      if (pending.current != null) {
        window.clearTimeout(pending.current);
      }
    };
  }, [value, intervalMs]);

  return throttled;
}
