import { useEffect, useState } from "react";

/**
 * Resolve background Python sidecar PID from Electron IPC or health payload.
 */
export function useSidecarPid(fallbackPid) {
  const [pid, setPid] = useState(
    fallbackPid != null && Number.isFinite(Number(fallbackPid))
      ? Number(fallbackPid)
      : null,
  );

  useEffect(() => {
    let cancelled = false;
    const resolve = async () => {
      try {
        if (window.apexIPC?.getSidecarStatus) {
          const status = await window.apexIPC.getSidecarStatus();
          const p = Number(status?.pid ?? status?.sidecarPid);
          if (!cancelled && Number.isFinite(p) && p > 0) {
            setPid(p);
            return;
          }
        }
      } catch {
        /* dev browser */
      }
      if (
        !cancelled &&
        fallbackPid != null &&
        Number.isFinite(Number(fallbackPid)) &&
        Number(fallbackPid) > 0
      ) {
        setPid(Number(fallbackPid));
      }
    };
    resolve();
    const id = window.setInterval(resolve, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [fallbackPid]);

  return pid;
}
