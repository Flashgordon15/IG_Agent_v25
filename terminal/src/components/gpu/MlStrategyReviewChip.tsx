"use client";

/**
 * Sober ML strategy review chip — silent on EDGE_OK / missing,
 * neutral/amber text row for NOT_MEASURABLE / APP_BLOCKED / NO_EDGE / EDGE_WEAK.
 * Never a green success badge.
 */

import { useEffect, useState } from "react";

type Chip = {
  visible: boolean;
  verdict: string;
  label: string;
  summary: string;
  tone: string;
};

const HIDDEN: Chip = {
  visible: false,
  verdict: "",
  label: "",
  summary: "",
  tone: "neutral",
};

export function MlStrategyReviewChip({ pollMs = 30000 }: { pollMs?: number }) {
  const [chip, setChip] = useState<Chip>(HIDDEN);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const res = await fetch("/api/desk/ml_strategy_review", {
          cache: "no-store",
        });
        const data = (await res.json()) as {
          chip?: Chip;
        };
        if (!alive) return;
        if (!data.chip || !data.chip.visible) {
          setChip(HIDDEN);
          return;
        }
        setChip({
          visible: true,
          verdict: String(data.chip.verdict || ""),
          label: String(data.chip.label || ""),
          summary: String(data.chip.summary || ""),
          tone: String(data.chip.tone || "neutral"),
        });
      } catch {
        /* keep last */
      }
    };
    void pull();
    const id = window.setInterval(pull, pollMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [pollMs]);

  if (!chip.visible) return null;

  const color =
    chip.tone === "amber"
      ? "rgba(201, 162, 39, 0.92)"
      : "rgba(160, 168, 180, 0.88)";

  return (
    <div
      className="ml-strategy-review-chip"
      title={chip.summary || chip.label}
      style={{
        marginTop: 6,
        padding: "4px 0",
        fontSize: 11,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        color,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        opacity: 0.95,
      }}
      data-verdict={chip.verdict}
    >
      {chip.label}
    </div>
  );
}
