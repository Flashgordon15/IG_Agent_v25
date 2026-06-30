import type { ButtonHTMLAttributes, ReactNode } from "react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { cn } from "../../lib/utils";

export function TooltipProvider({ children }: { children: ReactNode }) {
  return (
    <Tooltip.Provider delayDuration={300}>{children}</Tooltip.Provider>
  );
}

export function Tip({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          className="z-50 rounded border border-border bg-surface-elevated px-2 py-1 text-[11px] text-text shadow-lg"
          sideOffset={4}
        >
          {label}
          <Tooltip.Arrow className="fill-surface-elevated" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

export function Button({
  className,
  variant = "default",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "accent" | "danger" | "ghost";
}) {
  return (
    <button
      type="button"
      className={cn(
        "rounded border px-3 py-1 text-xs transition-all duration-200 disabled:opacity-50",
        variant === "default" &&
          "border-border bg-surface-elevated text-text-secondary hover:text-text",
        variant === "accent" &&
          "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20",
        variant === "danger" &&
          "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20",
        variant === "ghost" && "border-transparent text-text-secondary hover:text-text",
        className,
      )}
      {...props}
    />
  );
}
