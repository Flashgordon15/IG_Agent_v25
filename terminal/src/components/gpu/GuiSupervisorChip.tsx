"use client";

/**
 * Thin chip under SYSTEM OPERATIONAL — silent green on PASS,
 * amber WATCH / red FAIL/HALTED with plain alert tags
 * (BLEED / MICRO_HOLD / GUI_LIE / HALTED).
 */

import { useEffect, useState } from "react";

type ChipPayload = {
  visible: boolean;
  score: string;
  label: string;
  summary: string;
  tone: string;
  state_path: string;
  alerts?: string[];
  needs_code?: boolean;
  needs_ops?: boolean;
};

type ApiResp = {
  ok?: boolean;
  score?: string;
  checked_at?: string;
  top_finding?: { title?: string; detail?: string } | null;
  chip?: ChipPayload;
};

const SILENT: ChipPayload = {
  visible: false,
  score: "PASS",
  label: "SUPERVISOR PASS",
  summary: "",
  tone: "green",
  alerts: [],
  state_path: "src/data/v31-production/state/gui_supervisor_latest.json",
};

function normalizeAlerts(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    const tag = String(item || "")
      .trim()
      .toUpperCase();
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    out.push(tag);
  }
  return out;
}

export function GuiSupervisorChip({ pollMs = 15000 }: { pollMs?: number }) {
  const [chip, setChip] = useState<ChipPayload>(SILENT);
  const [detail, setDetail] = useState<string>("");

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const res = await fetch("/api/desk/gui_supervisor", {
          cache: "no-store",
        });
        const data = (await res.json()) as ApiResp;
        if (!alive) return;
        const next = data.chip;
        if (!next) {
          setChip(SILENT);
          setDetail("");
          return;
        }
        const score = String(next.score || "UNKNOWN").toUpperCase();
        const alerts = normalizeAlerts(next.alerts);
        setChip({
          visible: Boolean(next.visible),
          score,
          label: String(next.label || `SUPERVISOR ${score}`),
          summary: String(next.summary || ""),
          tone: String(next.tone || "green"),
          alerts,
          state_path: String(
            next.state_path ||
              "src/data/v31-production/state/gui_supervisor_latest.json",
          ),
          needs_code: Boolean(next.needs_code),
          needs_ops: Boolean(next.needs_ops),
        });
        setDetail(String(data.top_finding?.detail || ""));
      } catch {
        if (!alive) return;
        /* keep last chip on transient fetch errors */
      }
    };
    void pull();
    const id = window.setInterval(pull, pollMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [pollMs]);

  const halted =
    chip.score === "HALTED" ||
    (chip.alerts || []).includes("HALTED") ||
    (chip.alerts || []).includes("BLEED");
  if (!chip.visible || (chip.score === "PASS" && !halted)) {
    return null;
  }

  const alerts = chip.alerts || [];
  const title = [
    chip.label,
    alerts.length ? `alerts=${alerts.join(",")}` : null,
    chip.summary,
    detail,
    `see ${chip.state_path}`,
    chip.needs_code ? "needs_code=true" : null,
    chip.needs_ops ? "needs_ops=true" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="gui-supervisor-chip-wrap" aria-live="polite">
      <span
        className="gui-supervisor-chip"
        data-tone={chip.tone === "green" && halted ? "red" : chip.tone}
        title={title}
        role="status"
      >
        <span className="gui-supervisor-chip-label">{chip.label}</span>
        {alerts.length ? (
          <span className="gui-supervisor-chip-alerts" aria-label="supervisor alerts">
            {alerts.map((tag) => (
              <span key={tag} className="gui-supervisor-chip-alert" data-alert={tag}>
                {tag}
              </span>
            ))}
          </span>
        ) : null}
        {chip.summary ? (
          <span className="gui-supervisor-chip-summary gpu-ledger-mono">
            {chip.summary}
          </span>
        ) : null}
      </span>
    </div>
  );
}
