"use client";

import type { LucideIcon } from "lucide-react";
import { useFormatter } from "next-intl";
import type { ReactNode } from "react";

import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

/** The rows of the kit's trace (agent-trace.tsx): an icon on the rail, a head, its time, a body under the head. */

export function useTimes() {
  const format = useFormatter();
  // A phone's row is narrow: its times drop the seconds, which the tooltip keeps.
  const mobile = useIsMobile();
  return (at: string) => {
    const date = new Date(at);
    return {
      short: format.dateTime(date, { timeStyle: mobile ? "short" : "medium" }),
      full: format.dateTime(date, { dateStyle: "full", timeStyle: "long" }),
    };
  };
}

export function Time({ at }: { at: string }) {
  const times = useTimes();
  const { short, full } = times(at);
  return (
    <time dateTime={at} title={full} className="mt-0.5 font-mono text-xs leading-4 whitespace-nowrap text-fg-subtle tabular-nums">
      {short}
    </time>
  );
}

export type Look = "agent" | "user" | "tool" | "error" | "ask" | "live" | "system";

const ICON_LOOK: Record<Look, string> = {
  agent: "text-brand",
  user: "text-foreground",
  tool: "text-muted-foreground",
  error: "text-danger",
  ask: "text-attention",
  live: "text-running",
  system: "text-fg-subtle",
};

/**
 * One event of the kit's trace: a 16 px icon in its look's colour, no frame and no rail (the rows' left edge is the
 * icons' column), a head, the time on the right, and a body under the head. A system line (a move, a note of the hub)
 * is a caption with no time: the timeline has the times of the moves.
 */
export function Row({ icon: Icon, look, head, at, children, system = false }: { icon: LucideIcon; look: Look; head: ReactNode; at: string | null; children?: ReactNode; system?: boolean }) {
  return (
    <div className={cn("grid grid-cols-[16px_minmax(0,1fr)_auto] items-start gap-x-2.5 gap-y-1.5", system ? "py-1" : "py-2")}>
      <Icon className={cn(system ? "mt-px size-3.5" : "mt-0.5 size-4", ICON_LOOK[look])} aria-hidden="true" />
      <div className={cn("flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5", system ? "min-h-4 text-xs leading-4 text-fg-subtle" : "min-h-5 text-sm leading-5 text-muted-foreground")}>
        {head}
      </div>
      {at && !system ? <Time at={at} /> : <span />}
      {children ? <div className="col-span-2 col-start-2 min-w-0">{children}</div> : null}
    </div>
  );
}

export function Who({ children }: { children: ReactNode }) {
  return <span className="font-semibold text-foreground">{children}</span>;
}
