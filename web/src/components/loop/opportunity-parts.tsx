"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { useDay } from "@/components/data/facts";
import { Skeleton } from "@/components/ui/skeleton";

import { daysLeft } from "./opportunity-model";

/** A card of the opportunity's page: its h2 with an optional figure or link on the right, then what it holds. */
export function Panel({ id, title, aside, children, testId }: { id: string; title: string; aside?: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <section aria-labelledby={id} className="min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <div className="flex min-h-12 flex-wrap items-center gap-2 border-b px-4 py-2">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {title}
        </h2>
        {aside ? <div className="ml-auto flex min-w-0 items-center gap-2 text-[13px] text-fg-subtle">{aside}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** A card's rows while they load: two lines in the shape of a row's title and its second line. */
export function PanelSkeleton() {
  return (
    <div className="flex flex-col gap-2 px-4 py-3">
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-4 w-3/4" />
    </div>
  );
}

/** How many days a window has left, in words: "12 days left", "Closes today", "Closed 3 days ago"; none without a date. */
export function useDaysLeftText(): (windowUntil: string | null) => string | null {
  const t = useTranslations("opportunities.window");
  const [now] = useState(() => Date.now());
  return (windowUntil) => {
    const days = daysLeft(windowUntil, now);
    if (days === null) return null;
    if (days < 0) return t("past", { count: -days });
    if (days === 0) return t("today");
    return t("left", { count: days });
  };
}

/** The end of a window as a day, with the days it has left. */
export function WindowText({ windowUntil, windowNote }: { windowUntil: string | null; windowNote: string | null }) {
  const t = useTranslations("opportunities.window");
  const day = useDay();
  const left = useDaysLeftText();
  if (!windowUntil) return <>{windowNote ?? t("none")}</>;
  return (
    <>
      <span data-testid="opportunity-window-until">{day(windowUntil)}</span>
      <span className="text-muted-foreground" data-testid="opportunity-days-left">
        {" "}
        ({left(windowUntil)})
      </span>
      {windowNote ? <span className="block text-[13px] text-muted-foreground">{windowNote}</span> : null}
    </>
  );
}
