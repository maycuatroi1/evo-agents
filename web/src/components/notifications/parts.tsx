"use client";

import type { LucideIcon } from "lucide-react";
import { type ReactNode, useId } from "react";

import { cn } from "@/lib/utils";

/**
 * A card of the notification settings page, as Home's cards are drawn (web/DESIGN.md, Home): a 48 px head with the
 * h2 and what sits on its right, then the card's body. `className` places it in the page's grid.
 */
export function SettingsCard({
  title,
  action,
  children,
  className,
  testId,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  testId: string;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className={cn("min-w-0 rounded-md border bg-card shadow-raised", className)} data-testid={testId}>
      <div className="flex min-h-12 flex-wrap items-center gap-x-2 gap-y-1 border-b px-4 py-2">
        <h2 id={headingId} className="text-[15px] leading-[22px] font-semibold">
          {title}
        </h2>
        {action ? <div className="ml-auto flex items-center gap-2">{action}</div> : null}
      </div>
      {/* Its own box, so the first row of the body draws no rule against the head's. */}
      <div className="min-w-0">{children}</div>
    </section>
  );
}

/** The state of push in this browser as one block: an icon in its tone, a title, a line or two, then what to do. */
export function StateBlock({
  icon: Icon,
  tone = "neutral",
  title,
  children,
  actions,
  testId,
  state,
}: {
  icon: LucideIcon;
  tone?: "neutral" | "success" | "attention" | "danger";
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
  testId: string;
  state: string;
}) {
  return (
    <div className="flex flex-col gap-3 px-4 py-4" data-testid={testId} data-state={state}>
      <div className="flex min-w-0 items-start gap-3">
        <span
          className={cn(
            "flex size-8 shrink-0 items-center justify-center rounded-full",
            tone === "neutral" && "bg-muted text-muted-foreground",
            tone === "success" && "bg-success-soft text-success",
            tone === "attention" && "bg-attention-soft text-attention",
            tone === "danger" && "bg-danger-soft text-danger",
          )}
          aria-hidden="true"
        >
          <Icon className="size-4" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <p className="text-sm font-medium text-pretty text-foreground" data-testid={`${testId}-title`}>
            {title}
          </p>
          {children ? <div className="text-[13px] leading-[18px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">{children}</div> : null}
        </div>
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2 sm:pl-11">{actions}</div> : null}
    </div>
  );
}
