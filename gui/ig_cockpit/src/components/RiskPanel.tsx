import * as Dialog from "@radix-ui/react-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import { useState } from "react";
import { useCockpit } from "../hooks/CockpitProvider";
import type { StatusLevel } from "../types/cockpit";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Controls";
import { cn } from "../lib/utils";

const badgeVariant: Record<StatusLevel, "success" | "warning" | "danger"> = {
  ok: "success",
  warn: "warning",
  error: "danger",
};

export default function RiskPanel() {
  const {
    riskItems,
    riskAlerts,
    riskFlags,
    envelopeState,
    orderValve,
  } = useCockpit();
  const [govOpen, setGovOpen] = useState(false);

  return (
    <div className="panel flex min-h-0 flex-1 flex-col overflow-hidden gpu-layer">
      {orderValve === "suppressed" && (
        <div className="border-b border-danger/50 bg-danger/15 px-3 py-2 animate-pulse-soft">
          <p className="text-xs font-semibold text-danger">ORDER VALVE SUPPRESSED</p>
          <p className="text-[10px] text-danger/80">
            Hard enforcement or trading pause — no new orders
          </p>
        </div>
      )}
      {envelopeState === "breached" && (
        <div className="border-b border-warning/40 bg-warning/10 px-3 py-2">
          <p className="text-xs font-medium text-warning">Envelope breached</p>
          <p className="text-[10px] text-text-secondary">
            Hard enforcement active — review alerts tab
          </p>
        </div>
      )}

      <div className="panel-header">
        <span>Risk &amp; Governance</span>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] normal-case">
            {envelopeState} · valve {orderValve}
          </span>
          {riskFlags.length > 0 && (
            <Button variant="ghost" className="py-0 text-[10px]" onClick={() => setGovOpen(true)}>
              Flags ({riskFlags.length})
            </Button>
          )}
        </div>
      </div>

      <Tabs.Root defaultValue="limits" className="flex min-h-0 flex-1 flex-col">
        <Tabs.List className="flex border-b border-border px-2">
          {(["limits", "flags", "alerts"] as const).map((tab) => (
            <Tabs.Trigger
              key={tab}
              value={tab}
              className="px-3 py-2 text-xs capitalize text-text-secondary transition-colors data-[state=active]:border-b-2 data-[state=active]:border-accent data-[state=active]:text-text"
            >
              {tab === "limits" ? "Safety" : tab}
            </Tabs.Trigger>
          ))}
        </Tabs.List>

        <Tabs.Content value="limits" className="min-h-0 flex-1 overflow-y-auto p-2">
          <ul className="space-y-2">
            {riskItems.map((item) => (
              <li
                key={item.label}
                className={cn(
                  "rounded border px-2 py-2 transition-colors duration-300 hover:bg-surface-elevated/30",
                  item.label === "Order valve" && item.status === "error"
                    ? "border-danger/50 bg-danger/10"
                    : "border-border",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs text-text">{item.label}</div>
                  <Badge variant={badgeVariant[item.status]}>{item.status}</Badge>
                </div>
                <div className="mt-1 font-mono text-[11px] text-text-secondary">
                  {item.value}
                </div>
                <p className="mt-1 text-[10px] leading-snug text-muted">
                  {item.explanation}
                </p>
              </li>
            ))}
          </ul>
        </Tabs.Content>

        <Tabs.Content value="flags" className="min-h-0 flex-1 overflow-y-auto p-2">
          {riskFlags.length === 0 ? (
            <p className="text-xs text-text-secondary">No governance flags active.</p>
          ) : (
            <ul className="space-y-2">
              {riskFlags.map((flag) => (
                <li
                  key={flag.label}
                  className="rounded border border-accent/20 bg-accent/5 px-2 py-2 text-xs"
                >
                  <div className="font-medium text-accent">{flag.label}</div>
                  <p className="mt-1 text-[10px] text-text-secondary">{flag.detail}</p>
                </li>
              ))}
            </ul>
          )}
        </Tabs.Content>

        <Tabs.Content value="alerts" className="min-h-0 flex-1 overflow-y-auto p-2">
          {riskAlerts.length === 0 ? (
            <p className="text-xs text-text-secondary">No active alerts.</p>
          ) : (
            <ul className="space-y-2">
              {riskAlerts.map((alert) => (
                <li
                  key={alert}
                  className="rounded border border-danger/30 bg-danger/10 px-2 py-2 text-xs text-danger"
                >
                  {alert}
                </li>
              ))}
            </ul>
          )}
        </Tabs.Content>
      </Tabs.Root>

      <Dialog.Root open={govOpen} onOpenChange={setGovOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 max-h-[70vh] w-[min(480px,92vw)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-border bg-surface p-4 shadow-xl animate-slide-in">
            <Dialog.Title className="text-sm font-semibold text-text">
              Governance Flags
            </Dialog.Title>
            <Dialog.Description className="mt-1 text-xs text-text-secondary">
              Advisory flags from strategy_governance (read-only).
            </Dialog.Description>
            <ul className="mt-4 space-y-2">
              {riskFlags.map((flag) => (
                <li key={flag.label} className="rounded border border-border px-3 py-2">
                  <div className="text-sm font-medium text-accent">{flag.label}</div>
                  <p className="mt-1 text-xs text-text-secondary">{flag.detail}</p>
                </li>
              ))}
            </ul>
            <div className="mt-4 flex justify-end">
              <Dialog.Close asChild>
                <Button>Close</Button>
              </Dialog.Close>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
