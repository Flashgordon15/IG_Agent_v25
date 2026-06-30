/**
 * IG demo rate-limit banner — execution paused, analysis continues.
 */

import { memo, useCallback, useEffect, useState } from "react";
import { fetchIgBudgetState } from "../lib/api";

function IgBudgetBannerInner() {
  const [limited, setLimited] = useState(false);
  const [remaining, setRemaining] = useState(0);
  const [budget, setBudget] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const snap = await fetchIgBudgetState();
      setLimited(Boolean(snap.rate_limited || snap.execution_paused));
      setRemaining(Number(snap.cooldown_seconds_remaining) || 0);
      setBudget(Number(snap.estimated_budget_remaining) || 0);
    } catch {
      /* non-blocking */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, [refresh]);

  if (!limited) {
    return null;
  }

  const mins = Math.floor(remaining / 60);
  const secs = remaining % 60;

  return (
    <div
      className="border-b border-amber-500/40 bg-amber-500/15 px-3 py-1.5 text-center text-[11px] font-medium text-amber-200"
      role="status"
    >
      IG demo rate-limited — execution paused, analysis continues. Resume in{" "}
      {mins}:{secs.toString().padStart(2, "0")} · est. budget {budget}/48 (30m)
    </div>
  );
}

export default memo(IgBudgetBannerInner);
