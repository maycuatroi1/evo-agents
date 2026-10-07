"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { durationParts } from "@/components/runs/model";
import { countText } from "@/components/shell/nav";
import { Ago } from "@/components/workers/ago";
import { cn } from "@/lib/utils";

/** The pieces every card of Home is made of (web/DESIGN.md, Home): the card and its head, a row, a count, a time. */

/** A count drawn as a round pill in its tone; its words are said by the caller. */
export function CountPill({ value, tone, testId }: { value: number; tone: "attention" | "running" | "neutral"; testId?: string }) {
  return (
    <span
      aria-hidden="true"
      data-testid={testId}
      className={cn(
        "inline-flex h-4.5 min-w-4.5 shrink-0 items-center justify-center rounded-full px-1.25 text-[11px] leading-none font-medium tabular-nums",
        tone === "attention" && "bg-attention-soft text-attention",
        tone === "running" && "bg-running-soft text-running",
        tone === "neutral" && "bg-muted text-muted-foreground",
      )}
    >
      {countText(value)}
    </span>
  );
}

/** The kit's card with a head: a 48 px head (the h2, its count, a link on the right) above the card's rows. */
export function HomeCard({
  title,
  count,
  action,
  children,
  testId,
}: {
  title: string;
  count?: { value: number; tone: "attention" | "running" | "neutral"; words: string } | null;
  action?: ReactNode;
  children: ReactNode;
  testId: string;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="@container min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <div className="flex min-h-12 flex-wrap items-center gap-x-2 gap-y-1 border-b px-4 py-2">
        <h2 id={headingId} className="text-[15px] leading-[22px] font-semibold">
          {title}
        </h2>
        {count && count.value > 0 ? (
          <>
            <CountPill value={count.value} tone={count.tone} testId={`${testId}-count`} />
            <span className="sr-only">{count.words}</span>
          </>
        ) : null}
        {action ? <div className="ml-auto flex items-center gap-2">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

export const CARD_LINK = "rounded-xs text-[13px] font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:py-3";

/**
 * One row of a card's list (the kit's `eh-li`): a 20 px mark centred on the title and its facts, the title over one
 * line of facts, and what ends the row on the right. In a card narrower than 576 px (a phone, or the column beside Fleet
 * at 1024 px) the end moves under the facts, so the title keeps the width; the mark stays centred on the text.
 */
export function Row({ mark, title, sub, end, testId, data }: { mark: ReactNode; title: ReactNode; sub: ReactNode; end?: ReactNode; testId: string; data?: Record<string, string | number> }) {
  return (
    <li
      className="grid grid-cols-[20px_minmax(0,1fr)] items-center gap-x-3 gap-y-2 border-t px-4 py-3 transition-colors first:border-t-0 hover:bg-accent @xl:grid-cols-[20px_minmax(0,1fr)_auto]"
      data-testid={testId}
      {...Object.fromEntries(Object.entries(data ?? {}).map(([key, value]) => [`data-${key}`, value]))}
    >
      <span className="flex justify-center">{mark}</span>
      <div className="flex min-w-0 flex-col gap-0.5">
        <div className="flex min-w-0 items-center gap-2 text-sm leading-5 font-medium">{title}</div>
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs leading-4 text-fg-subtle">{sub}</div>
      </div>
      {end ? (
        <div className="col-start-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-subtle tabular-nums @xl:col-start-auto @xl:justify-end">
          {end}
        </div>
      ) : null}
    </li>
  );
}

/** "12 min", "1 h 5 min" or "under 1 min", as the runs pages say a duration. */
export function useDuration() {
  const t = useTranslations("runs.duration");
  return (ms: number) => {
    const { hours, minutes } = durationParts(ms);
    if (hours > 0) return t("hours", { hours, minutes });
    return minutes > 0 ? t("minutes", { minutes }) : t("lessThanMinute");
  };
}

/** The pulsing dot of an agent at work: the only one in its row. */
export function LiveDot() {
  return (
    <span className="relative inline-flex size-2 rounded-full bg-running" aria-hidden="true">
      <span className="absolute inset-0 animate-live-ping rounded-full bg-running" />
    </span>
  );
}

export function Relative({ value }: { value: string }) {
  return <Ago value={value} never="-" />;
}
